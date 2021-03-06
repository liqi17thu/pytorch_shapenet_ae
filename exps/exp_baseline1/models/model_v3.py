import torch
from torch import nn
import torch.nn.functional as F
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '../utils'))
from cd.chamfer import chamfer_distance
from emd.emd import earth_mover_distance

from pointnet2_ops import pointnet2_utils
from pointnet2_ops.pointnet2_modules import PointnetFPModule, PointnetSAModule
from pointnet2.models.pointnet2_ssg_cls import PointNet2ClassificationSSG


class PointNet2(PointNet2ClassificationSSG):
    def _build_model(self):
        self.SA_modules = nn.ModuleList()
        self.SA_modules.append(
            PointnetSAModule(
                npoint=512,
                radius=0.2,
                nsample=64,
                mlp=[3, 64, 64, 128],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=128,
                radius=0.4,
                nsample=64,
                mlp=[128, 128, 128, 256],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                mlp=[256, 256, 256, 256],
                use_xyz=True,
            )
        )

        self.fc_layer2 = nn.Sequential(
            nn.Linear(256, self.hparams['feat_dim']),
            nn.BatchNorm1d(self.hparams['feat_dim']),
            nn.ReLU(True),
        )

    def forward(self, pointcloud):
        r"""
            Forward pass of the network

            Parameters
            ----------
            pointcloud: Variable(torch.cuda.FloatTensor)
                (B, N, 3 + input_channels) tensor
                Point cloud to run predicts on
                Each point in the point-cloud MUST
                be formated as (x, y, z, features...)
        """
        xyz, features = self._break_up_pc(pointcloud)

        l_xyz, l_features = [xyz], [features]
        for i in range(len(self.SA_modules)):
            li_xyz, li_features = self.SA_modules[i](l_xyz[i], l_features[i])
            l_xyz.append(li_xyz)
            l_features.append(li_features)

        bottleneck_feats = l_features[-1].squeeze(-1)

        return self.fc_layer2(bottleneck_feats)


class Sampler(nn.Module):

    def __init__(self, probabilistic=True):
        super(Sampler, self).__init__()
        self.probabilistic = probabilistic

        self.mlp2mu = nn.Linear(128, 128)
        self.mlp2var = nn.Linear(128, 128)

    def forward(self, x):
        mu = self.mlp2mu(x)

        if self.probabilistic:
            logvar = self.mlp2var(x)
            std = logvar.mul(0.5).exp_()
            eps = torch.randn_like(std)

            kld = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)

            return torch.cat([eps.mul(std).add_(mu), kld], 1)
        else:
            return mu


class SampleDecoder(nn.Module):

    def __init__(self):
        super(SampleDecoder, self).__init__()
        self.mlp1 = nn.Linear(128, 128)

    def forward(self, input_feature):
        output = torch.relu(self.mlp1(input_feature))
        return output


class FCDecoder(nn.Module):

    def __init__(self, num_point=2048):
        super(FCDecoder, self).__init__()
        print('Using FCDecoder-NoBN!')

        self.mlp1 = nn.Linear(128, 1024)
        self.mlp2 = nn.Linear(1024, 1024)
        self.mlp3 = nn.Linear(1024, num_point*3)

    def forward(self, feat):
        batch_size = feat.shape[0]

        net = feat
        net = torch.relu(self.mlp1(net))
        net = torch.relu(self.mlp2(net))
        net = self.mlp3(net).view(batch_size, -1, 3)

        return net


class FCUpconvDecoder(nn.Module):

    def __init__(self, num_point=2048):
        super(FCUpconvDecoder, self).__init__()
        print('Using FCUpconvDecoder-NoBN!')

        self.mlp1 = nn.Linear(128, 1024)
        self.mlp2 = nn.Linear(1024, 1024)
        self.mlp3 = nn.Linear(1024, 1024*3)

        self.deconv1 = nn.ConvTranspose2d(128, 1024, 2, 1)
        self.deconv2 = nn.ConvTranspose2d(1024, 512, 3, 1)
        self.deconv3 = nn.ConvTranspose2d(512, 256, 4, 2)
        self.deconv4 = nn.ConvTranspose2d(256, 128, 5, 3)
        self.deconv5 = nn.ConvTranspose2d(128, 3, 1, 1)

    def forward(self, feat):
        batch_size = feat.shape[0]

        fc_net = feat
        fc_net = torch.relu(self.mlp1(fc_net))
        fc_net = torch.relu(self.mlp2(fc_net))
        fc_net = self.mlp3(fc_net).view(batch_size, -1, 3)

        upconv_net = feat.view(batch_size, -1, 1, 1)
        upconv_net = torch.relu(self.deconv1(upconv_net))
        upconv_net = torch.relu(self.deconv2(upconv_net))
        upconv_net = torch.relu(self.deconv3(upconv_net))
        upconv_net = torch.relu(self.deconv4(upconv_net))
        upconv_net = self.deconv5(upconv_net).view(batch_size, 3, -1).permute(0, 2, 1)
        
        net = torch.cat([fc_net, upconv_net], dim=1)

        return net


class Network(nn.Module):

    def __init__(self, conf):
        super(Network, self).__init__()
        self.conf = conf

        self.encoder = PointNet2({'feat_dim': 128})

        self.sample_encoder = Sampler(probabilistic=conf.probabilistic)
        self.sample_decoder = SampleDecoder()

        if conf.decoder_type == 'fc':
            self.decoder = FCDecoder(num_point=conf.num_point)

        elif conf.decoder_type == 'fcupconv':
            self.decoder = FCUpconvDecoder(num_point=conf.num_point)

        else:
            raise ValueError('ERROR: unknown decoder_type %s!' % decoder_type)

    """
        Input: B x N x 3
        Output: B x N x 3, B x F
    """
    def forward(self, input_pcs):
        feats = self.encoder(input_pcs.repeat(1, 1, 2))
        feats = self.sample_encoder(feats)
        ret_list = dict()
        if self.conf.probabilistic:
            feats, obj_kldiv_loss = torch.chunk(feats, 2, 1)
            ret_list['kldiv_loss'] = -obj_kldiv_loss.sum(dim=1)
        feats = self.sample_decoder(feats)
        output_pcs = self.decoder(feats)
        return output_pcs, feats, ret_list
     
    def infer(self, zs):
        feats = self.sample_decoder(zs)
        output_pcs = self.decoder(feats)
        return output_pcs
    
    def get_loss(self, pc1, pc2):
        if self.conf.loss_type == 'cd':
            dist1, dist2 = chamfer_distance(pc1, pc2, transpose=False)
            loss_per_data = torch.mean(dist1, dim=1) + torch.mean(dist2, dim=1)

        elif self.conf.loss_type == 'emd':
            loss_per_data = earth_mover_distance(pc1, pc2, transpose=False) / min(pc1.shape[1], pc2.shape[1])

        else:
            raise ValueError('ERROR: unknown loss_type %s!' % loss_type)

        return loss_per_data*100
    
