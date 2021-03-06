import os.path as pt

# noinspection PyUnresolvedReferences
import simplejpeg
import torch
from torch.backends import cudnn
from torch import nn
from torch import distributed as dist
from datadings.reader import Cycler
from torch.optim import SGD
from math import ceil, floor
from datetime import datetime
from datadings.reader import Cycler
from datetime import datetime
import numpy as np
from crumpets.torch.utils import Unpacker
from crumpets.torch.loss import CrossEntropyLoss, L1Loss
from crumpets.torch.policy import PolyPolicy
from crumpets.torch.dataloader import TorchTurboDataLoader
from readers import YFCC100mReader
from segnet import SegNet as Net
from worker import FCNWorker
from distributed import distribute
from crumpets.presets import NO_AUGMENTATION, AUGMENTATION_TRAIN
import os , sys
from sacred import Experiment
from sacred.observers import file_storage
from trainer import Trainer
from crumpets.presets import IMAGENET_MEAN, IMAGENET_STD


def expname(script=None):
    if script is None:
        script = sys.argv[0]
    return pt.splitext(pt.basename(script))[0]

ROOT = pt.abspath(pt.dirname(__file__))
#ROOT = pt.join(ROOT, '..')
EXP_FOLDER = pt.join(ROOT, '../exp')
exp = Experiment(' Experiment: AE with Classifier training')


# contruct the loader which will give the batches like a dataloader
# this function should also divide the data for each process
def make_loader(
        file,
        batch_size,
        device,
        world_size,
        rank,
        nworkers,  
        size = 96,
        image_rng=None,
        image_params=None,
        gpu_augmentation=False,
        nsamples = 10000
):

    # total images are 94502424
    
    # basically an iterator which cycles over the samples of msgpack
    reader = YFCC100mReader()
    # these many iterations for each dataloader on gpu
    iters = int(ceil(len(reader) / world_size /batch_size))  

    # now reader seeks to the iterator index because dataloader will have images from this index depending on the rank
    print('Number of iters ', iters)
    reader.seek(iters * batch_size * rank)
    print('\n start_iteration for device {} is {}'.format(device ,iters * batch_size * rank))

    cycler = Cycler(reader)
    
    # crumpets workers expect data to be in msgpack packed dictionaries, this should be done using datalodings library
    worker = FCNWorker(
        ((9*3, size, size), np.uint8, 0),
        ((9*3, size, size), np.uint8, 0),
        image_params=image_params,
        target_image_params=None,
        image_rng=image_rng,
    )

    return TorchTurboDataLoader(
        cycler.rawiter(), batch_size,
        worker, 1, shared_memory = 0
    )


def make_policy(epochs, network, lr, momentum):
    optimizer = SGD([
        {'params': network.parameters(), 'lr': lr},
    ], momentum=momentum, weight_decay=1e-4)

    # this creates a scheduler which is used to adjust the learning rate, many of the scheduler are defined , choose one as per your need
    # check if the policy is right
    scheduler = PolyPolicy(optimizer, epochs, 1)
    return optimizer, scheduler


# noinspection PyUnusedLocal
@exp.config
def config():
    # number of ranks across all nodes
    world_size = 2
    # first rank on this node
    first_rank = None
    # number of local ranks
    ranks = 2
    # rank of current process
    rank = 0
    # init method, usually TCP address
    init_method = None
    # dataset directory path
    datadir = '/ds2/YFCC100m/image_packs'
    # prefix to use for outdir, joined with {world_size}gpu_{lrscale}
    outdir_prefix = None
    # snapshot directory
    outdir = pt.join(ROOT, '../exp/logs_and_snapshot')
    # batch size per rank
    batch_size = 32
    # batch size per rank
    val_batch_size = 125
    # number of workers per rank
    num_workers = 1
    # learning rate
    lr = 0.001
    # length of warmup period in epochs
    warmup = 5
    # l2 regularization aka weight decay
    wd = 1e-4
    # bn momentum
    bn_momentum = 0.1
    # bn momentum correction
    bn_correct = True
    # decay bn momentum linearly
    num_epochs = 2
    # number of training epochs
    resume = None
    # finetune this snapshot
    finetune = None,
    size = 96,
    nsamples = 100000




log_location = os.path.join(EXP_FOLDER, os.path.basename(sys.argv[0])[:-3])
if len(exp.observers) == 0:
    print('Adding a file observer in %s' % log_location)
    exp.observers.append(file_storage.FileStorageObserver.create(log_location))


@exp.automain
@distribute
def main(
        _run,
        _config,
        world_size,
        rank,
        init_method,
        datadir,
        batch_size,
        val_batch_size,
        num_workers,
        outdir,
        outdir_prefix,
        lr,
        wd,
        bn_momentum,
        bn_correct,
        warmup,
        num_epochs,
        resume,
        finetune,
        size,
        nsamples
):
    cudnn.benchmark = True
    device = torch.device('cuda:0')  # device is set by CUDA_VISIBLE_DEVICES
    torch.cuda.set_device(device)

    # rank 0 creates experiment observer
    is_master = rank == 0

    # rank joins process group
    print('rank', rank, 'init_method', init_method)
    dist.init_process_group('nccl', rank=rank, world_size=world_size,
                            init_method=init_method)

    # actual training stuff
    train = make_loader(
        pt.join(datadir, '') if datadir else None,
        batch_size,device, world_size, rank, num_workers,size,
        # this the parameter based on which augmentation is applied to the data
        gpu_augmentation=False, image_rng=None, nsamples = nsamples
    )

    # lr is scaled linearly to original batch size of 256
    world_batch_size = world_size * batch_size
    k = world_batch_size / 256
    lr = k * lr

    # outdir stuff
    if outdir is None:
        outdir = pt.join(outdir_prefix, '%dgpu' % (world_size,))

    model = Net(num_classes = 1000, batch_size = batch_size)

    model = model.to(device)
    model = nn.parallel.DistributedDataParallel(model, device_ids=[device])
    #model = Unpacker(model)

    optimizer, policy = make_policy(num_epochs, model, lr, 0.9)
    print('\n policy defined')

    # loss for autoencoder
    loss = L1Loss(output_key = 'output' , target_key='target_image').to(device)
    # this loss is for classifier
    classifier_loss = CrossEntropyLoss(output_key='probs', target_key='label').to(device)
    trainer = Trainer(model, optimizer, loss, None, policy, None, train, None, outdir,
                      snapshot_interval=5 if is_master else None,
                      quiet=rank != 0)

    print('\n trainer has been initialized')

    start = datetime.now()
    with train:
        trainer.train(num_epochs, start_epoch= 0)

    print("Training complete in: " + str(datetime.now() - start))

