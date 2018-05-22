# -*- coding: utf-8 -*-
import numpy as np
import sys
import os
import pickle
import argparse
import math
import time
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torchvision.transforms as trn
import torchvision.datasets as dset
import torch.nn.functional as F
from torch.autograd import Variable as V

# go through rigamaroo to do ...utils.display_results import show_performance
if __package__ is None:
    import sys
    from os import path

    sys.path.append(path.dirname(path.dirname(path.dirname(path.abspath(__file__)))))
    from utils.cifar_resnet import WideResNet

parser = argparse.ArgumentParser(description='Trains a CIFAR-10 Classifier',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('dataset', type=str, choices=['cifar10', 'cifar100'],
                    help='Choose between CIFAR-10, CIFAR-100.')
# Optimization options
parser.add_argument('--epochs', '-e', type=int, default=100, help='Number of epochs to train.')
parser.add_argument('--learning_rate', '-lr', type=float, default=0.1, help='The initial learning rate.')
parser.add_argument('--batch_size', '-b', type=int, default=128, help='Batch size.')
parser.add_argument('--test_bs', type=int, default=200)
parser.add_argument('--momentum', '-m', type=float, default=0.9, help='Momentum.')
parser.add_argument('--decay', '-d', type=float, default=0.0005, help='Weight decay (L2 penalty).')
# Checkpoints
parser.add_argument('--save', '-s', type=str, default='./snapshots', help='Folder to save checkpoints.')
parser.add_argument('--load', '-l', type=str, default='', help='Checkpoint path to resume / test.')
parser.add_argument('--test', '-t', action='store_true', help='Test only flag.')
# Architecture
parser.add_argument('--layers', default=40, type=int, help='total number of layers (default: 28)')
parser.add_argument('--widen-factor', default=4, type=int, help='widen factor (default: 10)')
parser.add_argument('--droprate', default=0.3, type=float, help='dropout probability (default: 0.0)')
# Acceleration
parser.add_argument('--ngpu', type=int, default=1, help='0 = CPU.')
parser.add_argument('--prefetch', type=int, default=4, help='Pre-fetching threads.')
args = parser.parse_args()

torch.manual_seed(1)
np.random.seed(1)

state = {k: v for k, v in args._get_kwargs()}
state['tt'] = 0     # SGDR variable
state['init_learning_rate'] = args.learning_rate


# mean and standard deviation of channels of CIFAR-10 images
mean = [x / 255 for x in [125.3, 123.0, 113.9]]
std = [x / 255 for x in [63.0, 62.1, 66.7]]

train_transform = trn.Compose([trn.RandomHorizontalFlip(), trn.RandomCrop(32, padding=4),
                               trn.ToTensor(), trn.Normalize(mean, std)])
test_transform = trn.Compose([trn.ToTensor(), trn.Normalize(mean, std)])

if args.dataset == 'cifar10':
    train_data = dset.CIFAR10('/share/data/vision-greg/cifarpy', train=True, transform=test_transform, download=False)
    test_data = dset.CIFAR10('/share/data/vision-greg/cifarpy', train=False, transform=test_transform, download=False)
    num_classes = 10
else:
    train_data = dset.CIFAR100('/share/data/vision-greg/cifarpy', train=True, transform=test_transform, download=False)
    test_data = dset.CIFAR100('/share/data/vision-greg/cifarpy', train=False, transform=test_transform, download=False)
    num_classes = 100

train_loader = torch.utils.data.DataLoader(
    train_data, batch_size=args.batch_size, shuffle=True,
    num_workers=args.prefetch, pin_memory=True)
test_loader = torch.utils.data.DataLoader(
    test_data, batch_size=args.test_bs, shuffle=False,
    num_workers=args.prefetch, pin_memory=True)

# Create model
net = WideResNet(args.layers, num_classes, args.widen_factor, dropRate=args.droprate)

# Restore model
start_epoch = 0
if args.load != '':
    for i in range(1000 - 1, -1, -1):
        model_name = os.path.join(args.load, args.dataset + '_model_epoch' + str(i) + '.pytorch')
        if os.path.isfile(model_name):
            net.load_state_dict(torch.load(model_name))
            print('Model restored! Epoch:', i)
            start_epoch = i + 1
            break
    if start_epoch == 0:
        assert False, "could not resume"


if args.ngpu > 1:
    net = torch.nn.DataParallel(net, device_ids=list(range(args.ngpu)))

if args.ngpu > 0:
    net.cuda()
    torch.cuda.manual_seed(1)

cudnn.benchmark = True  # fire on all cylinders


optimizer = torch.optim.SGD(net.parameters(),
            state['learning_rate'], momentum=state['momentum'],
            weight_decay=state['decay'], nesterov=True)

from tqdm import tqdm

def train():
    net.train()  # enter train mode
    loss_avg = 0.0
    for batch_idx, (data, target) in enumerate(train_loader):

        data, target = V(data.cuda()), V(target.cuda())

        # forward
        x = net(data)

        # backward
        optimizer.zero_grad()
        loss = F.cross_entropy(x, target)
        loss.backward()
        optimizer.step()

        # exponential moving average
        loss_avg = loss_avg * 0.8 + loss.data[0] * 0.2

        dt = math.pi / float(args.epochs)
        state['tt'] += float(dt) / (len(train_loader.dataset) / float(args.batch_size))
        if state['tt'] >= math.pi - 0.01:
            state['tt'] = math.pi - 0.01
        curT = math.pi / 2.0 + state['tt']
        new_lr = args.learning_rate * (1.0 + math.sin(curT)) / 2.0  # lr_min = 0, lr_max = lr
        state['learning_rate'] = new_lr
        for param_group in optimizer.param_groups:
            param_group['lr'] = state['learning_rate']

    state['train_loss'] = loss_avg


# test function
def test():
    net.eval()
    loss_avg = 0.0
    correct = 0
    for batch_idx, (data, target) in enumerate(test_loader):
        data, target = V(data.cuda(), volatile=True), V(target.cuda(), volatile=True)

        # forward
        output = net(data)
        loss = F.cross_entropy(output, target)

        # accuracy
        pred = output.data.max(1)[1]
        correct += pred.eq(target.data).sum()

        # test loss average
        loss_avg += loss.data[0]

    state['test_loss'] = loss_avg / len(test_loader)
    state['test_accuracy'] = correct / len(test_loader.dataset)


if args.test:
    test()
    print(state)
    exit()

state['learning_rate'] = state['init_learning_rate']

print('Beginning Training')
# Main loop
best_accuracy = 0.0
for epoch in range(start_epoch, args.epochs):
    for param_group in optimizer.param_groups:
        param_group['lr'] = state['learning_rate']
    state['tt'] = math.pi / float(args.epochs) * epoch

    state['epoch'] = epoch

    begin_epoch = time.time()
    train()
    print('Epoch', epoch, '| Time Spent:', round(time.time() - begin_epoch, 4))

    test()

    torch.save(net.state_dict(),
               os.path.join(args.save, args.dataset + '_model_epoch' + str(epoch) + '.pytorch'))
    # Let us not waste space and delete the previous model
    # We do not overwrite the model because we need the epoch number
    try: os.remove(os.path.join(args.save, args.dataset + '_model_epoch' + str(epoch - 1) + '.pytorch'))
    except: True

    print(state)
