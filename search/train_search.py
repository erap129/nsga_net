import sys
# update your projecty root path before running
from search.micro_encoding import make_micro_creator

sys.path.insert(0, '/path/to/nsga-net')

import os
import numpy as np
import torch
import logging
import argparse
import torch.nn as nn
import torch.utils
# import torchvision.datasets as dset
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms

from models.macro_models import EvoNetwork
from models.micro_models import NetworkCIFAR as Network

import search.get_data as my_cifar10

import time
from misc import utils
from search import micro_encoding
from search import macro_encoding
from misc.flops_counter import add_flops_counting_methods
from config import config_dict, set_config

device = 'cuda'


def main(macro_genome, micro_genome, epochs, search_space='micro',
         save='Design_1', expr_root='search', seed=0, gpu=0, init_channels=24,
         layers=11, auxiliary=False, cutout=False, drop_path_prob=0.0, batch_size=128):

    # ---- train logger ----------------- #
    save_pth = os.path.join(expr_root, '{}'.format(save))
    utils.create_exp_dir(save_pth)
    log_format = '%(asctime)s %(message)s'
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format=log_format, datefmt='%m/%d %I:%M:%S %p')

    # ---- parameter values setting ----- #
    CIFAR_CLASSES = config_dict()['n_classes']
    INPUT_CHANNELS = config_dict()['n_channels']
    learning_rate = 0.025
    momentum = 0.9
    weight_decay = 3e-4
    data_root = '../data'
    cutout_length = 16
    auxiliary_weight = 0.4
    grad_clip = 5
    report_freq = 50
    train_params = {
        'auxiliary': auxiliary,
        'auxiliary_weight': auxiliary_weight,
        'grad_clip': grad_clip,
        'report_freq': report_freq,
    }

    if search_space == 'micro' or search_space == 'micro_garbage':
        genome = micro_genome
        genotype = micro_encoding.decode(genome)
        model = Network(init_channels, CIFAR_CLASSES, config_dict()['n_channels'], layers, auxiliary, genotype)
    elif search_space == 'macro' or search_space == 'macro_garbage':
        genome = macro_genome
        genotype = macro_encoding.decode(genome)
        channels = [(INPUT_CHANNELS, init_channels),
                    (init_channels, 2*init_channels),
                    (2*init_channels, 4*init_channels)]
        model = EvoNetwork(genotype, channels, CIFAR_CLASSES, (config_dict()['INPUT_HEIGHT'], config_dict()['INPUT_WIDTH']), decoder='residual')
    elif search_space == 'micromacro':
        genome = [macro_genome, micro_genome]
        macro_genotype = macro_encoding.decode(macro_genome)
        micro_genotype = micro_encoding.decode(micro_genome)
        genotype = [macro_genotype, micro_genotype]
        set_config('micro_creator', make_micro_creator(micro_genotype, convert=False))
        channels = [(INPUT_CHANNELS, init_channels),
                    (init_channels, 2 * init_channels),
                    (2 * init_channels, 4 * init_channels)]
        model = EvoNetwork(macro_genotype, channels, CIFAR_CLASSES,
                           (config_dict()['INPUT_HEIGHT'], config_dict()['INPUT_WIDTH']), decoder='residual')

    else:
        raise NameError('Unknown search space type')

    # logging.info("Genome = %s", genome)
    logging.info("Architecture = %s", genotype)

    torch.cuda.set_device(gpu)
    cudnn.benchmark = True
    torch.manual_seed(seed)
    cudnn.enabled = True
    torch.cuda.manual_seed(seed)

    n_params = (np.sum(np.prod(v.size()) for v in filter(lambda p: p.requires_grad, model.parameters())) / 1e6)
    model = model.to(device)

    logging.info("param size = %fMB", n_params)

    if config_dict()['problem'] == 'classification':
        criterion = nn.CrossEntropyLoss()
    else:
        criterion = nn.MSELoss()
    criterion = criterion.cuda()


    parameters = filter(lambda p: p.requires_grad, model.parameters())
    optimizer = torch.optim.SGD(
        parameters,
        learning_rate,
        momentum=momentum,
        weight_decay=weight_decay
    )

    CIFAR_MEAN = [0.49139968, 0.48215827, 0.44653124]
    CIFAR_STD = [0.24703233, 0.24348505, 0.26158768]

    train_transform = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor()
    ])

    if cutout:
        train_transform.transforms.append(utils.Cutout(cutout_length))

    train_transform.transforms.append(transforms.Normalize(CIFAR_MEAN, CIFAR_STD))

    valid_transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
    ])

    train_data = my_cifar10.CIFAR10(root=data_root, train=True, download=False, transform=train_transform)
    valid_data = my_cifar10.CIFAR10(root=data_root, train=False, download=False, transform=valid_transform)

    train_queue = torch.utils.data.DataLoader(
        train_data, batch_size=batch_size,
        # sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[:split]),
        pin_memory=True, num_workers=1)

    valid_queue = torch.utils.data.DataLoader(
        valid_data, batch_size=batch_size,
        # sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[split:num_train]),
        pin_memory=True, num_workers=1)

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, int(epochs))

    for epoch in range(epochs):
        scheduler.step()
        logging.info('epoch %d lr %e', epoch, scheduler.get_lr()[0])
        model.droprate = drop_path_prob * epoch / epochs

        train_acc, train_obj = train(train_queue, model, criterion, optimizer, train_params)
        logging.info(f'train_{config_dict()["performance_measure"]} %f', train_acc)

    valid_acc, valid_obj = infer(valid_queue, model, criterion)
    logging.info(f'valid_{config_dict()["performance_measure"]} %f', valid_acc)

    # calculate for flops
    model = add_flops_counting_methods(model)
    model.eval()
    model.start_flops_count()
    random_data = torch.randn(1, INPUT_CHANNELS, config_dict()['INPUT_HEIGHT'], config_dict()['INPUT_WIDTH'])
    model(torch.autograd.Variable(random_data).to(device))
    n_flops = np.round(model.compute_average_flops_cost() / 1e6, 4)
    logging.info('flops = %f', n_flops)

    # save to file
    # os.remove(os.path.join(save_pth, 'log.txt'))
    with open(os.path.join(save_pth, 'log.txt'), "w") as file:
        file.write("Genome = {}\n".format(genome))
        file.write("Architecture = {}\n".format(genotype))
        file.write("param size = {}MB\n".format(n_params))
        file.write("flops = {}MB\n".format(n_flops))
        file.write("valid_acc = {}\n".format(valid_acc))

    # logging.info("Architecture = %s", genotype))

    return {
        'valid_acc': valid_acc,
        'params': n_params,
        'flops': n_flops,
    }


# Training
def train(train_queue, net, criterion, optimizer, params):
    net.train()
    train_loss = 0
    correct = 0
    total = 0

    for step, (inputs, targets) in enumerate(train_queue):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs, outputs_aux = net(inputs)
        if config_dict()['problem'] == 'regression':
            targets = targets.float()
        loss = criterion(outputs, targets)

        if params['auxiliary']:
            loss_aux = criterion(outputs_aux, targets)
            loss += params['auxiliary_weight'] * loss_aux

        loss.backward()
        nn.utils.clip_grad_norm_(net.parameters(), params['grad_clip'])
        optimizer.step()

        if config_dict()['problem'] == 'classification':
            train_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()
        else:
            train_loss -= loss.item()
            total += targets.size(0)

    if config_dict()['problem'] == 'classification':
        return 100.*correct/total, train_loss/total
    else:
        return train_loss / total, train_loss / total


def infer(valid_queue, net, criterion):
    net.eval()
    test_loss = 0
    correct = 0
    total = 0

    with torch.no_grad():
        for step, (inputs, targets) in enumerate(valid_queue):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs, _ = net(inputs)
            if config_dict()['problem'] == 'regression':
                targets = targets.float()
            loss = criterion(outputs, targets)

            if config_dict()['problem'] == 'classification':
                test_loss += loss.item()
                _, predicted = outputs.max(1)
                total += targets.size(0)
                correct += predicted.eq(targets).sum().item()
            else:
                test_loss -= loss.item()
                total += targets.size(0)

            # if step % args.report_freq == 0:
            #     logging.info('valid %03d %e %f', step, test_loss/total, 100.*correct/total)
    if config_dict()['problem'] == 'classification':
        acc = 100. * correct / total
        return acc, test_loss / total
    else:
        return test_loss / total, test_loss / total



if __name__ == "__main__":
    DARTS_V2 = [[[[3, 0], [3, 1]], [[3, 0], [3, 1]], [[3, 1], [2, 0]], [[2, 0], [5, 2]]],
               [[[0, 0], [0, 1]], [[2, 2], [0, 1]], [[0, 0], [2, 2]], [[2, 2], [0, 1]]]]
    start = time.time()
    print(main(genome=DARTS_V2, epochs=20, save='DARTS_V2_16', seed=1, init_channels=16,
               auxiliary=False, cutout=False, drop_path_prob=0.0))
    print('Time elapsed = {} mins'.format((time.time() - start)/60))
    # start = time.time()
    # print(main(genome=DARTS_V2, epochs=20, save='DARTS_V2_32', seed=1, init_channels=32))
    # print('Time elapsed = {} mins'.format((time.time() - start) / 60))