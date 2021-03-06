"""
    For models: model_v1_x.py
"""

import os
import time
import sys
import shutil
import random
from time import strftime
from argparse import ArgumentParser
import numpy as np
import torch
import torch.utils.data

torch.multiprocessing.set_sharing_strategy('file_system')
from subprocess import call
from casual_part_dataset import CasualPartDataset
import utils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

import logging

logger = logging.getLogger("trimesh")
logger.setLevel(logging.ERROR)


def train(conf):
    # create training and validation datasets and data loaders
    train_dataset = CasualPartDataset(no_casual_num=0, self_casual_num=2, binary_casual_num=1)
    utils.printout(conf.flog, str(train_dataset))
    train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=conf.batch_size, shuffle=True,
                                                   pin_memory=False, \
                                                   num_workers=conf.num_workers, drop_last=True,
                                                   collate_fn=utils.collate_feats, worker_init_fn=utils.worker_init_fn)

    val_dataset = CasualPartDataset(no_casual_num=0, self_casual_num=2, binary_casual_num=1)
    utils.printout(conf.flog, str(val_dataset))
    val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=conf.batch_size, shuffle=False,
                                                 pin_memory=False, \
                                                 num_workers=0, drop_last=True, collate_fn=utils.collate_feats,
                                                 worker_init_fn=utils.worker_init_fn)

    # load network model
    model_def = utils.get_model_module(conf.model_version)

    # create models
    network = model_def.CasualNetwork(conf)
    if conf.checkpoint is not None:
        network.load_state_dict(torch.load(conf.checkpoint))
    utils.printout(conf.flog, '\n' + str(network) + '\n')

    models = [network]
    model_names = ['network']

    # create optimizers
    network_opt = torch.optim.Adam(network.parameters(), lr=conf.lr, weight_decay=conf.weight_decay)
    optimizers = [network_opt]
    optimizer_names = ['network_opt']

    # learning rate scheduler
    network_lr_scheduler = torch.optim.lr_scheduler.StepLR(network_opt, step_size=conf.lr_decay_every,
                                                           gamma=conf.lr_decay_by)

    # create logs
    if not conf.no_console_log:
        header = '     Time    Epoch     Dataset    Iteration    Progress(%)       LR   TotalLoss   Accuracy    SRC_IOU     TGT_IOU'
    if not conf.no_tb_log:
        # https://github.com/lanpa/tensorboard-pytorch
        from tensorboardX import SummaryWriter
        train_writer = SummaryWriter(os.path.join(conf.exp_dir, 'train'))
        val_writer = SummaryWriter(os.path.join(conf.exp_dir, 'val'))

    # send parameters to device
    for m in models:
        m.to(conf.device)
    for o in optimizers:
        utils.optimizer_to_device(o, conf.device)

    # start training
    start_time = time.time()

    last_checkpoint_step = None
    last_train_console_log_step, last_val_console_log_step = None, None
    train_num_batch = len(train_dataloader)
    val_num_batch = len(val_dataloader)

    # train for every epoch
    for epoch in range(conf.epochs):
        if not conf.no_console_log:
            utils.printout(conf.flog, f'training run {conf.exp_name}')
            utils.printout(conf.flog, header)

        train_batches = enumerate(train_dataloader, 0)
        val_batches = enumerate(val_dataloader, 0)
        train_fraction_done = 0.0
        val_fraction_done = 0.0
        val_batch_ind = -1

        # train for every batch
        for train_batch_ind, batch in train_batches:
            train_fraction_done = (train_batch_ind + 1) / train_num_batch
            train_step = epoch * train_num_batch + train_batch_ind

            log_console = not conf.no_console_log and (last_train_console_log_step is None or \
                                                       train_step - last_train_console_log_step >= conf.console_log_interval)
            if log_console:
                last_train_console_log_step = train_step

            # set models to training mode
            for m in models:
                m.train()

            # forward pass (including logging)
            total_loss = forward(batch=batch, network=network, conf=conf, is_val=False, \
                                 step=train_step, epoch=epoch, batch_ind=train_batch_ind, num_batch=train_num_batch,
                                 start_time=start_time, \
                                 log_console=log_console, log_tb=not conf.no_tb_log, tb_writer=train_writer,
                                 lr=network_opt.param_groups[0]['lr'])

            # optimize one step
            network_opt.zero_grad()
            total_loss.backward()
            network_opt.step()
            network_lr_scheduler.step()

            # save checkpoint
            with torch.no_grad():
                if last_checkpoint_step is None or train_step - last_checkpoint_step >= conf.checkpoint_interval:
                    utils.printout(conf.flog, 'Saving checkpoint ...... ')
                    utils.save_checkpoint(models=models, model_names=model_names,
                                          dirname=os.path.join(conf.exp_dir, 'ckpts'), \
                                          epoch=epoch, prepend_epoch=True, optimizers=optimizers,
                                          optimizer_names=model_names)
                    utils.printout(conf.flog, 'DONE')
                    last_checkpoint_step = train_step

            # validate one batch
            while val_fraction_done <= train_fraction_done and val_batch_ind + 1 < val_num_batch:
                val_batch_ind, val_batch = next(val_batches)

                val_fraction_done = (val_batch_ind + 1) / val_num_batch
                val_step = (epoch + val_fraction_done) * train_num_batch - 1

                log_console = not conf.no_console_log and (last_val_console_log_step is None or \
                                                           val_step - last_val_console_log_step >= conf.console_log_interval)
                if log_console:
                    last_val_console_log_step = val_step

                # set models to evaluation mode
                for m in models:
                    m.eval()

                with torch.no_grad():
                    # forward pass (including logging)
                    __ = forward(batch=val_batch, network=network, conf=conf, is_val=True, \
                                 step=val_step, epoch=epoch, batch_ind=val_batch_ind, num_batch=val_num_batch,
                                 start_time=start_time, \
                                 log_console=log_console, log_tb=not conf.no_tb_log, tb_writer=val_writer,
                                 lr=network_opt.param_groups[0]['lr'])
        train_dataset.reset()
        val_dataset.reset()

    # save the final models
    utils.printout(conf.flog, 'Saving final checkpoint ...... ')
    utils.save_checkpoint(models=models, model_names=model_names, dirname=os.path.join(conf.exp_dir, 'ckpts'), \
                          epoch=epoch, prepend_epoch=False, optimizers=optimizers, optimizer_names=optimizer_names)
    utils.printout(conf.flog, 'DONE')


def forward(batch, network, conf, \
            is_val=False, step=None, epoch=None, batch_ind=0, num_batch=1, start_time=0, \
            log_console=False, log_tb=False, tb_writer=None, lr=None):
    # prepare input
    src_idx, dst_idx, src_pc, dst_pc, src_gt, dst_gt = batch
    src_idx = torch.stack(src_idx, dim=0).to(conf.device)
    dst_idx = torch.stack(dst_idx, dim=0).to(conf.device)
    src_pc = torch.stack(src_pc, dim=0).to(conf.device)
    dst_pc = torch.stack(dst_pc, dim=0).to(conf.device)
    src_gt = torch.stack(src_gt, dim=0).to(conf.device)
    dst_gt = torch.stack(dst_gt, dim=0).to(conf.device)

    batch_size = src_pc.shape[0]

    # forward through the network
    relation, full, src_pred, dst_pred = network(src_idx, dst_idx, src_pc, dst_pc)  # B x N x 3, B x P
    src_pred = src_pred.squeeze()
    dst_pred = dst_pred.squeeze()

    # for each type of loss, compute losses per data
    loss, accuracy, src_iou, tgt_iou = network.get_loss(relation, full, src_pred, src_gt, dst_pred, dst_gt)

    # display information
    data_split = 'train'
    if is_val:
        data_split = 'val'

    with torch.no_grad():
        # log to console
        if log_console:
            utils.printout(conf.flog, \
                           f'''{strftime("%H:%M:%S", time.gmtime(time.time() - start_time)):>9s} '''
                           f'''{epoch:>5.0f}/{conf.epochs:<5.0f} '''
                           f'''{data_split:^10s} '''
                           f'''{batch_ind:>5.0f}/{num_batch:<5.0f} '''
                           f'''{100. * (1 + batch_ind + num_batch * epoch) / (num_batch * conf.epochs):>9.1f}%      '''
                           f'''{lr:>5.2E} '''
                           f'''{loss.item():>10.5f}'''
                           f'''{accuracy.item():>10.5f}'''
                           f'''{src_iou.item():>10.5f}'''
                           f'''{tgt_iou.item():>10.5f}''')
            conf.flog.flush()

        # log to tensorboard
        if log_tb and tb_writer is not None:
            tb_writer.add_scalar('loss', loss.item(), step)
            tb_writer.add_scalar('lr', lr, step)

        # gen visu
        if is_val and (not conf.no_visu) and epoch % conf.num_epoch_every_visu == 0:
            visu_dir = os.path.join(conf.exp_dir, 'val_visu')
            out_dir = os.path.join(visu_dir, 'epoch-%04d' % epoch)
            input_pcs_dir = os.path.join(out_dir, 'input_pcs')
            output_pcs_dir = os.path.join(out_dir, 'output_pcs')
            info_dir = os.path.join(out_dir, 'info')

            if batch_ind == 0:
                # create folders
                os.mkdir(out_dir)
                os.mkdir(input_pcs_dir)
                os.mkdir(output_pcs_dir)
                os.mkdir(info_dir)

            if batch_ind < conf.num_batch_every_visu:
                utils.printout(conf.flog, 'Visualizing ...')

                for i in range(batch_size):
                    src_fn = 'data-src-%03d.png' % (batch_ind * batch_size + i)
                    dst_fn = 'data-dst-%03d.png' % (batch_ind * batch_size + i)

                    # render_pts(os.path.join(input_pcs_dir, fn), input_pcs[i].cpu().numpy())
                    # render_pts(os.path.join(output_pcs_dir, fn), output_pcs[i].cpu().numpy())
                    # or to render using matplotlib
                    utils.render_pc(os.path.join(input_pcs_dir, src_fn), src_pc[i].cpu().numpy(), src_gt[i].bool().cpu().numpy())
                    utils.render_pc(os.path.join(input_pcs_dir, dst_fn), dst_pc[i].cpu().numpy(), dst_gt[i].bool().cpu().numpy())

                    if relation[i] < 0.5:
                        utils.render_pc(os.path.join(output_pcs_dir, src_fn), src_pc[i].cpu().numpy(), np.zeros(src_pc[i].shape[0], dtype=bool))
                        utils.render_pc(os.path.join(output_pcs_dir, dst_fn), dst_pc[i].cpu().numpy(), np.zeros(dst_pc[i].shape[0], dtype=bool))
                    else:
                        utils.render_pc(os.path.join(output_pcs_dir, src_fn), src_pc[i].cpu().numpy(), (src_pred[i] >= 0.5).cpu().numpy())
                        if full[i] >= 0.5:
                            utils.render_pc(os.path.join(output_pcs_dir, dst_fn), dst_pc[i].cpu().numpy(), np.ones(dst_pc[i].shape[0], dtype=bool))
                        else:
                            utils.render_pc(os.path.join(output_pcs_dir, dst_fn), dst_pc[i].cpu().numpy(), (dst_pred[i] >= 0.5).cpu().numpy())

            if batch_ind == conf.num_batch_every_visu - 1:
                # visu html
                utils.printout(conf.flog, 'Generating html visualization ...')
                sublist = 'input_pcs,output_pcs,info'
                cmd = 'cd %s && python %s . 10 htmls %s %s > /dev/null' % (
                out_dir, os.path.join(BASE_DIR, '../utils/gen_html_hierarchy_local.py'), sublist, sublist)
                call(cmd, shell=True)
                utils.printout(conf.flog, 'DONE')

    return loss


if __name__ == '__main__':

    ### get parameters
    parser = ArgumentParser()

    # main parameters (required)
    parser.add_argument('--exp_suffix', type=str, help='exp suffix')
    parser.add_argument('--model_version', type=str, help='model def file')

    # main parameters (optional)
    parser.add_argument('--device', type=str, default='cuda:0', help='cpu or cuda:x for using cuda on GPU number x')
    parser.add_argument('--seed', type=int, default=3124256514,
                        help='random seed (for reproducibility) [specify -1 means to generate a random one]')
    # parser.add_argument('--seed', type=int, default=-1, help='random seed (for reproducibility) [specify -1 means to generate a random one]')
    parser.add_argument('--checkpoint', type=str)
    parser.add_argument('--log_dir', type=str, default='logs', help='exp logs directory')
    parser.add_argument('--data_dir', type=str, help='data directory')
    parser.add_argument('--val_data_dir', type=str, help='data directory')
    parser.add_argument('--overwrite', action='store_true', default=False,
                        help='overwrite if exp_dir exists [default: False]')

    # network settings
    parser.add_argument('--num_point', type=int, default=2048)
    parser.add_argument('--decoder_type', type=str, default='fc')
    parser.add_argument('--loss_type', type=str, default='cd')
    parser.add_argument('--kldiv_loss_weight', type=float, default=1e-4)
    parser.add_argument('--probabilistic', action='store_true', default=False, help='probabilistic [default: False]')

    # training parameters
    parser.add_argument('--epochs', type=int, default=1000)
    parser.add_argument('--batch_size', type=int, default=16)
    parser.add_argument('--num_workers', type=int, default=10)
    parser.add_argument('--lr', type=float, default=.001)
    parser.add_argument('--weight_decay', type=float, default=1e-5)
    parser.add_argument('--lr_decay_by', type=float, default=0.9)
    parser.add_argument('--lr_decay_every', type=float, default=5000)

    # loss weights

    # logging
    parser.add_argument('--no_tb_log', action='store_true', default=False)
    parser.add_argument('--no_console_log', action='store_true', default=False)
    parser.add_argument('--console_log_interval', type=int, default=1,
                        help='number of optimization steps beween console log prints')
    parser.add_argument('--checkpoint_interval', type=int, default=10000,
                        help='number of optimization steps beween checkpoints')

    # visu
    parser.add_argument('--num_batch_every_visu', type=int, default=1, help='num batch every visu')
    parser.add_argument('--num_epoch_every_visu', type=int, default=10, help='num epoch every visu')
    parser.add_argument('--no_visu', action='store_true', default=False, help='no visu? [default: False]')

    # parse args
    conf = parser.parse_args()

    ### prepare before training
    # make exp_name
    conf.exp_name = f'exp-{conf.model_version}-{conf.exp_suffix}'

    # mkdir exp_dir; ask for overwrite if necessary
    conf.exp_dir = os.path.join(conf.log_dir, conf.exp_name)
    if os.path.exists(conf.exp_dir):
        if not conf.overwrite:
            response = input('A training run named "%s" already exists, overwrite? (y/n) ' % conf.exp_name)
            if response != 'y':
                exit(1)
        shutil.rmtree(conf.exp_dir)
    os.mkdir(conf.exp_dir)
    os.mkdir(os.path.join(conf.exp_dir, 'ckpts'))
    if not conf.no_visu:
        os.mkdir(os.path.join(conf.exp_dir, 'val_visu'))

    # control randomness
    if conf.seed < 0:
        conf.seed = random.randint(1, 10000)
    random.seed(conf.seed)
    np.random.seed(conf.seed)
    torch.manual_seed(conf.seed)

    # save config
    torch.save(conf, os.path.join(conf.exp_dir, 'conf.pth'))

    # file log
    flog = open(os.path.join(conf.exp_dir, 'train_log.txt'), 'w')
    conf.flog = flog

    # backup command running
    utils.printout(flog, ' '.join(sys.argv) + '\n')
    utils.printout(flog, f'Random Seed: {conf.seed}')

    # backup python files used for this training
    os.system('cp data.py models/%s.py %s %s' % (conf.model_version, __file__, conf.exp_dir))

    # set training device
    device = torch.device(conf.device)
    utils.printout(flog, f'Using device: {conf.device}\n')
    conf.device = device

    ### start training
    train(conf)

    ### before quit
    # close file log
    flog.close()

