import numpy as np
import math
import functools

import torch
import torch.nn as nn
from torch.nn import init
import torch.optim as optim
import torch.nn.functional as F
from torch.nn import Parameter as P

import layers
from sync_batchnorm import SynchronizedBatchNorm2d as SyncBatchNorm2d
from convgru import ConvGRULinear


# Architectures for G
# Attention is passed in in the format '32_64' to mean applying an attention
# block at both resolution 32x32 and 64x64. Just '64' will apply at 64x64.
def G_arch(ch=64, attention='64', ksize='333333', dilation='111111'):
  arch = {}
  arch[512] = {'in_channels' :  [ch * item for item in [16, 16, 8, 8, 4, 2, 1]],
               'out_channels' : [ch * item for item in [16,  8, 8, 4, 2, 1, 1]],
               'upsample' : [True] * 7,
               'resolution' : [8, 16, 32, 64, 128, 256, 512],
               'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
                              for i in range(3,10)}}
  arch[256] = {'in_channels' :  [ch * item for item in [16, 16, 8, 8, 4, 2]],
               'out_channels' : [ch * item for item in [16,  8, 8, 4, 2, 1]],
               'upsample' : [True] * 6,
               'resolution' : [8, 16, 32, 64, 128, 256],
               'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
                              for i in range(3,9)}}
  # arch[128] = {'in_channels' :  [ch * item for item in [16, 16, 8, 4, 2]],
  #              'out_channels' : [ch * item for item in [16, 8, 4, 2, 1]],
  #              'upsample' : [True] * 5,
  #              'resolution' : [8, 16, 32, 64, 128],
  #              'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
  #                             for i in range(3,8)}}

  #Acceleration architecture
  arch[128] = {'in_channels' :  [16 * item for item in [8, 8, 8, 4]],
               'out_channels' : [16 * item for item in [8, 8, 4, 1]],
               'upsample' : [True] * 3 + [False],
               'resolution' : [8, 16, 32, 32],
               'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
                              for i in range(3,7)}}


  arch[64]  = {'in_channels' :  [ch * item for item in [8, 8, 8, 4, 2]],
               'out_channels' : [ch * item for item in [8, 8, 4, 2, 1]],
               'upsample' : [True] * 4 + [False],
               'resolution' : [8, 16, 32, 64, 64],
               'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
                              for i in range(3,7)}}
  #Xiaodan: This is the original BigGAN architecture
  # arch[64]  = {'in_channels' :  [ch * item for item in [16, 16, 8, 4]],
  #              'out_channels' : [ch * item for item in [16, 8, 4, 2]],
  #              'upsample' : [True] * 4,
  #              'resolution' : [8, 16, 32, 64],
  #              'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
  #                             for i in range(3,7)}}

  arch[32]  = {'in_channels' :  [ch * item for item in [4, 4, 4]],
               'out_channels' : [ch * item for item in [4, 4, 4]],
               'upsample' : [True] * 3,
               'resolution' : [8, 16, 32],
               'attention' : {2**i: (2**i in [int(item) for item in attention.split('_')])
                              for i in range(3,6)}}

  return arch

class Generator(nn.Module):
  #xiaodan: time_steps added by xiaodan
  def __init__(self, G_ch=64, dim_z=128, bottom_width=4, resolution=128,
               G_kernel_size=3, G_attn='64', n_classes=1000, time_steps=12,
               num_G_SVs=1, num_G_SV_itrs=1,
               G_shared=True, shared_dim=0, hier=False,
               cross_replica=False, mybn=False,
               G_activation=nn.ReLU(inplace=False),
               G_lr=5e-5, G_B1=0.0, G_B2=0.999, adam_eps=1e-8,
               BN_eps=1e-5, SN_eps=1e-12, G_mixed_precision=False, G_fp16=False,
               G_init='ortho', skip_init=False, no_optim=False,
               G_param='SN', norm_style='bn',
               **kwargs):
    super(Generator, self).__init__()
    # Channel width mulitplier
    self.ch = G_ch
    # Dimensionality of the latent space
    self.dim_z = dim_z
    # The initial spatial dimensions
    self.bottom_width = bottom_width
    # Resolution of the output
    self.resolution = resolution
    # Kernel size?
    self.kernel_size = G_kernel_size
    # Attention?
    self.attention = G_attn
    # number of classes, for use in categorical conditional generation
    self.n_classes = n_classes
    # xiaodan: The number of frames we want to generate
    self.time_steps = time_steps
    # Use shared embeddings?
    self.G_shared = G_shared
    # Dimensionality of the shared embedding? Unused if not using G_shared
    self.shared_dim = shared_dim if shared_dim > 0 else dim_z
    # Hierarchical latent space?
    self.hier = hier
    # Cross replica batchnorm?
    self.cross_replica = cross_replica
    # Use my batchnorm?
    self.mybn = mybn
    # nonlinearity for residual blocks
    self.activation = G_activation
    # Initialization style
    self.init = G_init
    # Parameterization style
    self.G_param = G_param
    # Normalization style
    self.norm_style = norm_style
    # Epsilon for BatchNorm?
    self.BN_eps = BN_eps
    # Epsilon for Spectral Norm?
    self.SN_eps = SN_eps
    # fp16?
    self.fp16 = G_fp16
    # Architecture dict
    self.arch = G_arch(self.ch, self.attention)[resolution]
    # xiaodan: added these flags
    self.no_convgru = kwargs['no_convgru']
    self.no_Dv = kwargs['no_Dv']
    self.no_sepa_attn = kwargs['no_sepa_attn']
    self.no_full_attn = kwargs['no_full_attn']

    # If using hierarchical latents, adjust z
    if self.hier:
      # Number of places z slots into
      self.num_slots = len(self.arch['in_channels']) + 1
      self.z_chunk_size = (self.dim_z // self.num_slots)
      # Recalculate latent dimensionality for even splitting into chunks
      self.dim_z = self.z_chunk_size *  self.num_slots
    else:
      self.num_slots = 1
      self.z_chunk_size = 0

    # Which convs, batchnorms, and linear layers to use
    if self.G_param == 'SN':
      self.which_conv = functools.partial(layers.SNConv2d,
                          kernel_size=3, padding=1,
                          num_svs=num_G_SVs, num_itrs=num_G_SV_itrs,
                          eps=self.SN_eps)
      self.which_linear = functools.partial(layers.SNLinear,
                          num_svs=num_G_SVs, num_itrs=num_G_SV_itrs,
                          eps=self.SN_eps)
    else:
      self.which_conv = functools.partial(nn.Conv2d, kernel_size=3, padding=1)
      self.which_linear = nn.Linear

    # We use a non-spectral-normed embedding here regardless;
    # For some reason applying SN to G's embedding seems to randomly cripple G
    self.which_embedding = nn.Embedding
    bn_linear = (functools.partial(self.which_linear, bias=False) if self.G_shared
                 else self.which_embedding)
    #xiaodan : ccbn changed by xiaodan to ccbn5D; time_steps added by xiaodan
    self.which_bn = functools.partial(layers.ccbn5D,
                          which_linear=bn_linear,
                          time_steps = self.time_steps,
                          cross_replica=self.cross_replica,
                          mybn=self.mybn,
                          input_size=(self.shared_dim + self.z_chunk_size if self.G_shared
                                      else self.n_classes),
                          norm_style=self.norm_style,
                          eps=self.BN_eps)


    # Prepare model
    # If not using shared embeddings, self.shared is just a passthrough
    print('G_shared?', self.G_shared)
    self.shared = (self.which_embedding(n_classes, self.shared_dim) if G_shared
                    else layers.identity())
    # First linear layer
    self.linear = self.which_linear(self.dim_z // self.num_slots,
                                    self.arch['in_channels'][0] * (self.bottom_width **2))
    # xiaodan: convolutional GRU with linear transformation on top
    #xiaodan: for noise_size, not sure if shared_dim is the dim of y
    # xiaodan: not sure if ch0 should be self.arch['in_channels'][0]
    # xiaodan: hidden_dim is also output dimension, which should alse be ch0
    # print('dim_z:',self.dim_z,'shared_dim:',self.shared_dim)
    if self.no_convgru == False:
      convgru_noise_size = self.dim_z+self.shared_dim if self.G_shared else self.dim_z
      self.convgru = ConvGRULinear(noise_size=convgru_noise_size,
                                 ch0=self.arch['in_channels'][0],
                                 time_steps=self.time_steps,
                                 input_size=(self.bottom_width,self.bottom_width),
                                 hidden_dim=self.arch['in_channels'][0],
                                 kernel_size=(3,3),
                                 num_layers=1,
                                 dtype=torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor,
                                 batch_first=True)
    # xiaodan: full-attn layers
    # xiaodan: not sure if this is ch0
    if self.no_full_attn == False:
      print('Using full attention module...')
      self.fullAttn = layers.FullAttention(ch=self.arch['in_channels'][0],
                                         time_steps=self.time_steps)
                                         # which_conv = self.which_conv) #xiaodan: commented by xiaodan to use the default SNConv3d
    # self.blocks is a doubly-nested list of modules, the outer loop intended
    # to be over blocks at a given resolution (resblocks and/or self-attention)
    # while the inner loop is over a given block
    self.blocks = []
    for index in range(len(self.arch['out_channels'])):
      self.blocks += [[layers.GBlock(in_channels=self.arch['in_channels'][index],
                             out_channels=self.arch['out_channels'][index],
                             which_conv=self.which_conv,
                             which_bn=self.which_bn,
                             activation=self.activation,
                             upsample=(functools.partial(F.interpolate, scale_factor=2)
                                       if self.arch['upsample'][index] else None))]]

      # If attention on this block, attach it to the end
      if self.arch['attention'][self.arch['resolution'][index]]:
        if self.no_sepa_attn == False:
          # xiaodan: add three seperable attention layers in G at certain resolution
          print('Adding separable attention layer in G at resolution %d' % self.arch['resolution'][index])
          self.blocks[-1] += [layers.SelfAttention_width(self.arch['out_channels'][index], self.time_steps,self.which_conv)]
          self.blocks[-1] += [layers.SelfAttention_height(self.arch['out_channels'][index], self.time_steps,self.which_conv)]
          self.blocks[-1] += [layers.SelfAttention_time(self.arch['out_channels'][index], self.time_steps,self.which_conv)]

        else:
          print('Adding attention layer in G at resolution %d' % self.arch['resolution'][index])
          self.blocks[-1] += [layers.Attention(self.arch['out_channels'][index], self.which_conv)]


    # Turn self.blocks into a ModuleList so that it's all properly registered.
    self.blocks = nn.ModuleList([nn.ModuleList(block) for block in self.blocks])

    # output layer: batchnorm-relu-conv.
    # Consider using a non-spectral conv here
    self.output_layer = nn.Sequential(layers.bn(self.arch['out_channels'][-1],
                                                cross_replica=self.cross_replica,
                                                mybn=self.mybn),
                                    self.activation,
                                    self.which_conv(self.arch['out_channels'][-1], 3))

    # Initialize weights. Optionally skip init for testing.
    if not skip_init:
      self.init_weights()

    # Set up optimizer
    # If this is an EMA copy, no need for an optim, so just return now
    if no_optim:
      return
    self.lr, self.B1, self.B2, self.adam_eps = G_lr, G_B1, G_B2, adam_eps
    if G_mixed_precision:
      print('Using fp16 adam in G...')
      import utils
      self.optim = utils.Adam16(params=self.parameters(), lr=self.lr,
                           betas=(self.B1, self.B2), weight_decay=0,
                           eps=self.adam_eps)
    else:
      self.optim = optim.Adam(params=self.parameters(), lr=self.lr,
                           betas=(self.B1, self.B2), weight_decay=0,
                           eps=self.adam_eps)

    # LR scheduling, left here for forward compatibility
    # self.lr_sched = {'itr' : 0}# if self.progressive else {}
    # self.j = 0

  # Initialize
  def init_weights(self):
    self.param_count = 0
    for module in self.modules():
      if (isinstance(module, nn.Conv2d)
          or isinstance(module, nn.Linear)
          or isinstance(module, nn.Embedding)):
        if self.init == 'ortho':
          init.orthogonal_(module.weight)
        elif self.init == 'N02':
          init.normal_(module.weight, 0, 0.02)
        elif self.init in ['glorot', 'xavier']:
          init.xavier_uniform_(module.weight)
        else:
          print('Init style not recognized...')
        self.param_count += sum([p.data.nelement() for p in module.parameters()])
    print('Param count for G''s initialized parameters: %d' % self.param_count)

  # Note on this forward function: we pass in a y vector which has
  # already been passed through G.shared to enable easy class-wise
  # interpolation later. If we passed in the one-hot and then ran it through
  # G.shared in this forward function, it would be harder to handle.
  def forward(self, z, y):
    # print('z in G shape',z.shape)
    # y shape: [B,128], norm of each y is around 2.5 to 3
    # z shape: [B,128], norm of each z is around 10 to 11
    # print('z in G norm ',torch.norm(z,dim=1).mean())
    # print('y in G norm ',torch.norm(y,dim=1).mean())
    # If hierarchical, concatenate zs and ys
    if self.hier:
      zs = torch.split(z, self.z_chunk_size, 1)
      z = zs[0]
      ys = [torch.cat([y, item], 1) for item in zs[1:]]
    else:
      ys = [y] * len(self.blocks)
    # print(z.shape,y.shape, type(z),type(y))
    # xiaodan: concatenate z and y, then send into convgru
    if self.no_convgru == False:
      if self.G_shared:
        zy = torch.cat((z,y),1) # [B, 256]
      else:
        zy = z
      layer_output_list, last_state_list = self.convgru(zy)
      h = layer_output_list[-1] #[B,T,C,4,4]
      # h = h.contiguous().view(-1,*h.shape[2:]) #[BT,C,4,4]
    else:
      h = self.linear(z)
      # print('h size at 293',h.shape)
      h = h.contiguous().view(h.size(0), self.time_steps, -1, self.bottom_width, self.bottom_width) #[B, 1, C, 4, 4]
    # print('dim zy:',zy.shape)
    # First linear layer

    # print('H shape after convgru',h.shape)
    #xiaodan: send h into full Attention
    if self.no_full_attn == False:
      h = self.fullAttn(h)#[B,T,C,4,4]
    #Xiaodan: Moved out from the no_full_attn if statement by Xiaodan
    h = h.contiguous().view(-1,*h.shape[2:]) #[BT,C,4,4]
    # Loop over blocks
    for index, blocklist in enumerate(self.blocks):
      # Second inner loop in case block has multiple layers
      for block in blocklist:
        # ys_BT = ys[index].repeat(self.time_steps,1,1).permute(1,0,2).contiguous().view(-1,y.shape[-1])
        #xiaodan: added if else statement to account when G_shared==False
        if len(y.shape)>1:
            ys_BT = ys[index].repeat(self.time_steps,1,1).permute(1,0,2).contiguous().view(-1,y.shape[-1]) # [BT,128]
        else:
            ys_BT = ys[index].repeat(self.time_steps,1).permute(1,0).contiguous()
        # print('ys_BT shape in G',ys_BT.shape)
        # print('h first column',h[:,0,0,0])
        # print('ys_BT first column',ys_BT[:,0])
        # print('h norm',torch.norm(h.reshape(h.shape[0],-1),dim = 1).mean())

        h = block(h, ys_BT) #[BT,C,H,W]
        # print('ys_BT', ys_BT.get_device())
        # print('h', h.get_device())

    # Apply batchnorm-relu-conv-tanh at output
    return torch.tanh(self.output_layer(h)).contiguous().view(-1,self.time_steps,3,*h.shape[2:]) #[B,T,3,H,W]


# Discriminator architecture, same paradigm as G's above
def D_img_arch(ch=64, attention='64',ksize='333333', dilation='111111'):
  arch = {}
  arch[256] = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4, 8, 8, 16]],
               'out_channels' : [item * ch for item in [1, 2, 4, 8, 8, 16, 16]],
               'downsample' : [True] * 6 + [False],
               'resolution' : [128, 64, 32, 16, 8, 4, 4 ],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,8)}}
  arch[128] = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4, 8, 16]],
               'out_channels' : [item * ch for item in [1, 2, 4, 8, 16, 16]],
               'downsample' : [True] * 5 + [False],
               'resolution' : [64, 32, 16, 8, 4, 4],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,8)}}
  arch[64]  = {'in_channels' :  [3] + [ch*item for item in [2, 4, 8, 16]],
               'out_channels' : [item * ch for item in [2, 4, 8, 16, 16]],
               'downsample' : [True] * 4 + [False],
               'resolution' : [32, 16, 8, 4, 4],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,7)}}

  #Xiaodan: This is the original BigGAN architecture
  # arch[64]  = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4, 8]],
  #              'out_channels' : [item * ch for item in [1, 2, 4, 8, 16]],
  #              'downsample' : [True] * 4 + [False],
  #              'resolution' : [32, 16, 8, 4, 4],
  #              'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
  #                             for i in range(2,7)}}

  arch[32]  = {'in_channels' :  [3] + [item * ch for item in [4, 4, 4]],
               'out_channels' : [item * ch for item in [4, 4, 4, 4]],
               'downsample' : [True, True, False, False],
               'resolution' : [16, 8, 8, 8],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,6)}}
  return arch

def D_vid_arch(ch=64, attention='64',ksize='333333', dilation='111111'):
  arch = {}
  arch[256] = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4, 8, 8]],
               'out_channels' : [item * ch for item in [1, 2, 4, 8, 8, 16]],
               'downsample' : [True] * 5 + [False],
               'resolution' : [64, 32, 16, 8, 4, 4 ],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,7)},
                '3D block' :[True] * 2 + [False] * 4       }
  arch[128] = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4, 8]],
               'out_channels' : [item * ch for item in [1, 2, 4, 8, 16]],
               'downsample' : [True] * 4 + [False],
               'resolution' : [32, 16, 8, 4, 4],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,7)},
                '3D block' :[True] * 2 + [False] * 3       }
  arch[64]  = {'in_channels' :  [3] + [ch*item for item in [1, 2, 4]],
               'out_channels' : [item * ch for item in [1, 2, 4, 8]],
               'downsample' : [True] * 3 + [False],
               'resolution' : [16, 8, 4, 4],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,6)},
               '3D block' :[True] * 2 + [False] * 2       }

  arch[32]  = {'in_channels' :  [3] + [item * ch for item in [4, 4]],
               'out_channels' : [item * ch for item in [4, 4, 4]],
               'downsample' : [True, True, False],
               'resolution' : [8, 4, 4],
               'attention' : {2**i: 2**i in [int(item) for item in attention.split('_')]
                              for i in range(2,4)},
                '3D block' :[True] * 2 + [False]       }
  return arch

class ImageDiscriminator(nn.Module):

  def __init__(self, D_ch=64, D_wide=True, resolution=128,
               D_kernel_size=3, D_attn='64', n_classes=1000,
               num_D_SVs=1, num_D_SV_itrs=1, D_activation=nn.ReLU(inplace=False),
               D_lr=2e-4, D_B1=0.0, D_B2=0.999, adam_eps=1e-8,
               SN_eps=1e-12, output_dim=1, D_mixed_precision=False, D_fp16=False,
               D_init='ortho', skip_init=False, D_param='SN', **kwargs):
    super(ImageDiscriminator, self).__init__()
    # Width multiplier
    self.ch = D_ch
    # Use Wide D as in BigGAN and SA-GAN or skinny D as in SN-GAN?
    self.D_wide = D_wide
    # Resolution
    self.resolution = resolution
    # Kernel size
    self.kernel_size = D_kernel_size
    # Attention?
    self.attention = D_attn
    # Number of classes
    self.n_classes = n_classes
    # Activation
    self.activation = D_activation
    # Initialization style
    self.init = D_init
    # Parameterization style
    self.D_param = D_param
    # Epsilon for Spectral Norm?
    self.SN_eps = SN_eps
    # Fp16?
    self.fp16 = D_fp16
    # Architecture
    self.arch = D_img_arch(self.ch, self.attention)[resolution]

    # Which convs, batchnorms, and linear layers to use
    # No option to turn off SN in D right now
    if self.D_param == 'SN':
      self.InitDownsample = nn.AvgPool3d(kernel_size=(1,2,2),stride = (1,2,2))
      self.which_conv = functools.partial(layers.SNConv2d,
                          kernel_size=3, padding=1,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_linear = functools.partial(layers.SNLinear,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_embedding = functools.partial(layers.SNEmbedding,
                              num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                              eps=self.SN_eps)
    # Prepare model
    # self.blocks is a doubly-nested list of modules, the outer loop intended
    # to be over blocks at a given resolution (resblocks and/or self-attention)
    self.blocks = []
    for index in range(len(self.arch['out_channels'])):
      self.blocks += [[layers.DBlock(in_channels=self.arch['in_channels'][index],
                       out_channels=self.arch['out_channels'][index],
                       which_conv=self.which_conv,
                       wide=self.D_wide,
                       activation=self.activation,
                       preactivation=(index > 0),
                       downsample=(nn.AvgPool2d(2) if self.arch['downsample'][index] else None))]]
      # If attention on this block, attach it to the end
      if self.arch['attention'][self.arch['resolution'][index]]:
        print('Adding attention layer in D at resolution %d' % self.arch['resolution'][index])
        self.blocks[-1] += [layers.Attention(self.arch['out_channels'][index],
                                             self.which_conv)]
    # Turn self.blocks into a ModuleList so that it's all properly registered.
    self.blocks = nn.ModuleList([nn.ModuleList(block) for block in self.blocks])
    # Linear output layer. The output dimension is typically 1, but may be
    # larger if we're e.g. turning this into a VAE with an inference output
    self.linear = self.which_linear(self.arch['out_channels'][-1], output_dim)
    # Embedding for projection discrimination
    self.embed = self.which_embedding(self.n_classes, self.arch['out_channels'][-1])

    # Initialize weights
    if not skip_init:
      self.init_weights()

    # Set up optimizer
    self.lr, self.B1, self.B2, self.adam_eps = D_lr, D_B1, D_B2, adam_eps
    if D_mixed_precision:
      print('Using fp16 adam in D...')
      import utils
      self.optim = utils.Adam16(params=self.parameters(), lr=self.lr,
                             betas=(self.B1, self.B2), weight_decay=0, eps=self.adam_eps)
    else:
      self.optim = optim.Adam(params=self.parameters(), lr=self.lr,
                             betas=(self.B1, self.B2), weight_decay=0, eps=self.adam_eps)
    # LR scheduling, left here for forward compatibility
    # self.lr_sched = {'itr' : 0}# if self.progressive else {}
    # self.j = 0

  # Initialize
  def init_weights(self):
    self.param_count = 0
    for module in self.modules():
      if (isinstance(module, nn.Conv2d)
          or isinstance(module, nn.Linear)
          or isinstance(module, nn.Embedding)):
        if self.init == 'ortho':
          init.orthogonal_(module.weight)
        elif self.init == 'N02':
          init.normal_(module.weight, 0, 0.02)
        elif self.init in ['glorot', 'xavier']:
          init.xavier_uniform_(module.weight)
        else:
          print('Init style not recognized...')
        self.param_count += sum([p.data.nelement() for p in module.parameters()])
    print('Param count for D''s initialized parameters: %d' % self.param_count)

  def forward(self, x, y=None):
    # x shape: [B*2*k, 3, H, W] note: B*2 becuase we concatenate real and fake samples
    # if y.get_device() == 0:
    #   print('y in D',y)
    # print('x in D shape', x.shape)
    # Stick x into h for cleaner for loops without flow control
    h = x
    # Loop over blocks
    for index, blocklist in enumerate(self.blocks):
      for block in blocklist:
        h = block(h)
    # Apply global sum pooling as in SN-GAN
    h = torch.sum(self.activation(h), [2, 3])
    # Get initial class-unconditional output
    # print('h in D shape',h.shape)
    # print('h in D norm', torch.norm(h,dim=1).mean())
    out = self.linear(h)
    # print('out in D',out)
    # print('out', out.get_device())

    # print('y.shape image', y.shape)
    # print('embed(y) shape image', self.embed(y).shape)
    # print('h shape image', h.shape)
    # Get projection of final featureset onto class vectors and add to evidence
    # print('y,embed y, sum y',y.shape,self.embed(y).shape,torch.sum(self.embed(y) * h, 1, keepdim=True).shape)
    # print('torch sum of embed * h in D',torch.sum(self.embed(y) * h, 1, keepdim=True))
    out = out + torch.sum(self.embed(y) * h, 1, keepdim=True)
    return out
def init_weights(model, init_type='xavier', gain=0.02):
  '''
  initialize network's weights
  init_type: normal | xavier | kaiming | orthogonal
  '''

  def init_func(m):
    classname = m.__class__.__name__
    if hasattr(m, 'weight') and (classname.find('Conv') != -1 or classname.find('Linear') != -1):
      if init_type == 'normal':
        nn.init.normal_(m.weight.data, 0.0, gain)
      elif init_type == 'xavier':
#                nn.init.xavier_normal_(m.weight.data, gain=gain)
        nn.init.xavier_uniform_(m.weight.data)
      elif init_type == 'kaiming':
        nn.init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
      elif init_type == 'orthogonal':
        nn.init.orthogonal_(m.weight.data, gain=gain)

      if hasattr(m, 'bias') and m.bias is not None:
        nn.init.constant_(m.bias.data, 0.0)

    elif classname.find('BatchNorm2d') != -1:
      nn.init.normal_(m.weight.data, 1.0, gain)
      nn.init.constant_(m.bias.data, 0.0)

  model.apply(init_func)


class decoder(nn.Module):
  def __init__(self):
    super(encoder, self).__init__(time_steps=12)
    self.conv_dim = 64
    self.time_steps = time_steps
    self.decoder = nn.Sequential(
        nn.Conv2d(16, 256, 3, 1, 1),
        nn.ReLU(True),
        nn.Conv2d(256, 256, 3, 1, 1),
        nn.ReLU(True),
        nn.Conv2d(256, 256, 3, 1, 1),
        nn.ReLU(True),
        nn.Upsample(scale_factor=2, mode='nearest'),
        nn.Conv2d(256, 128, 3, 1, 1),
        nn.ReLU(True),
        nn.Conv2d(128, 128, 3, 1, 1),
        nn.ReLU(True),
        nn.Upsample(scale_factor=2, mode='nearest'),
        nn.Conv2d(128, 64, 3, 1, 1),
        nn.ReLU(True),
        nn.Conv2d(64, 3, 3, 1, 1),
        nn.Tanh()
      )
    self.apply(init_weights)

  def forward(self, feat):

    out = self.decoder(feat)
    out = out.contiguous().view(-1, self.time_steps, *out.shape[1:])
    return out

class VideoDiscriminator(nn.Module):

  def __init__(self, D_ch=64, D_wide=True, resolution=128,
               D_kernel_size=3, D_attn='64', n_classes=1000, time_steps = 12,
               num_D_SVs=1, num_D_SV_itrs=1, D_activation=nn.ReLU(inplace=False),
               D_lr=2e-4, D_B1=0.0, D_B2=0.999, adam_eps=1e-8,
               SN_eps=1e-12, output_dim=1, D_mixed_precision=False, D_fp16=False,
               D_init='ortho', skip_init=False, D_param='SN', **kwargs):
    super(VideoDiscriminator, self).__init__()
    # Width multiplier
    self.ch = D_ch
    # Use Wide D as in BigGAN and SA-GAN or skinny D as in SN-GAN?
    self.D_wide = D_wide
    # Resolution
    self.resolution = resolution
    # Kernel size
    self.kernel_size = D_kernel_size
    # Attention?
    self.attention = D_attn
    # Number of classes
    self.n_classes = n_classes
    #xiaodan: added time_steps here
    self.time_steps = time_steps
    # Activation
    self.activation = D_activation
    # Initialization style
    self.init = D_init
    # Parameterization style
    self.D_param = D_param
    # Epsilon for Spectral Norm?
    self.SN_eps = SN_eps
    # Fp16?
    self.fp16 = D_fp16
    # Architecture
    self.arch = D_vid_arch(self.ch, self.attention)[resolution]
    #Xiaodan: Added by xiaodan
    self.Dv_no_res = kwargs['Dv_no_res']
    self.T_into_B = kwargs['T_into_B']

    # Which convs, batchnorms, and linear layers to use
    # No option to turn off SN in D right now
    if self.D_param == 'SN':
      self.InitDownsample = nn.AvgPool3d(kernel_size=(1,2,2),stride = (1,2,2))
      self.which_conv = functools.partial(layers.SNConv2d,
                          kernel_size=3, padding=1,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_conv3d_1 = functools.partial(layers.SNConv3d,
                          kernel_size=3, padding=1, stride=2,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_conv3d_2 = functools.partial(layers.SNConv3d,
                          kernel_size=3, padding=1,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_linear = functools.partial(layers.SNLinear,
                          num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                          eps=self.SN_eps)
      self.which_embedding = functools.partial(layers.SNEmbedding,
                              num_svs=num_D_SVs, num_itrs=num_D_SV_itrs,
                              eps=self.SN_eps)
      self.conv3d_no_res = functools.partial(nn.Conv3d,
                            kernel_size=3, padding=1, stride=2
                            )
    # Prepare model
    # self.blocks is a doubly-nested list of modules, the outer loop intended
    # to be over blocks at a given resolution (resblocks and/or self-attention)
    self.blocks = []
    for index in range(len(self.arch['out_channels'])):
      if self.arch['3D block'][index]:
        if self.Dv_no_res == False:
          self.blocks += [[layers.BasicBlock(
                           in_planes=self.arch['in_channels'][index],
                           out_planes=self.arch['out_channels'][index],
                           which_conv1 = self.which_conv3d_1,
                           which_conv2 = self.which_conv3d_2,
                           downsample=(nn.Conv3d(
                                          self.arch['in_channels'][index],
                                          self.arch['out_channels'][index],
                                          kernel_size=1,
                                          stride=2,
                                          bias=False) if self.arch['downsample'][index] else None)
                            )]]
        else:
          print('Using 3D Conv layers instead of 3D resnet')
          self.blocks += [[layers.Conv3DBlock(
                           in_planes=self.arch['in_channels'][index],
                           out_planes=self.arch['out_channels'][index],
                           which_conv = self.conv3d_no_res
                           )]]
      else:
        self.blocks += [[layers.DBlock(in_channels=self.arch['in_channels'][index],
                       out_channels=self.arch['out_channels'][index],
                       which_conv=self.which_conv,
                       wide=self.D_wide,
                       activation=self.activation,
                       preactivation=(index > 0),
                       downsample=(nn.AvgPool2d(2) if self.arch['downsample'][index] else None))]]
          #xiaodan: disabled for video discriminator
          # If attention on this block, attach it to the end
          # if self.arch['attention'][self.arch['resolution'][index]]:
          #   print('Adding attention layer in D at resolution %d' % self.arch['resolution'][index])
          #   self.blocks[-1] += [layers.Attention(self.arch['out_channels'][index],
          #                                        self.which_conv)]
    # Turn self.blocks into a ModuleList so that it's all properly registered.
    self.blocks = nn.ModuleList([nn.ModuleList(block) for block in self.blocks])
    # Linear output layer. The output dimension is typically 1, but may be
    # larger if we're e.g. turning this into a VAE with an inference output
    t_dim_red_const = self.arch['3D block'].count(True) * 2
    even =  self.time_steps % 2
    self.reduced_t_dim = (self.time_steps // t_dim_red_const + even)

    # Embedding for projection discrimination
    if self.T_into_B == False:
      self.linear = self.which_linear(self.arch['out_channels'][-1] * self.reduced_t_dim, output_dim)
      self.embed = self.which_embedding(self.n_classes, self.arch['out_channels'][-1] * self.reduced_t_dim)
    else:
      self.linear = self.which_linear(self.arch['out_channels'][-1], output_dim)
      self.embed = self.which_embedding(self.n_classes, self.arch['out_channels'][-1])

    # Initialize weights
    if not skip_init:
      self.init_weights()

    # Set up optimizer
    self.lr, self.B1, self.B2, self.adam_eps = D_lr, D_B1, D_B2, adam_eps
    if D_mixed_precision:
      print('Using fp16 adam in D...')
      import utils
      self.optim = utils.Adam16(params=self.parameters(), lr=self.lr,
                             betas=(self.B1, self.B2), weight_decay=0, eps=self.adam_eps)
    else:
      self.optim = optim.Adam(params=self.parameters(), lr=self.lr,
                             betas=(self.B1, self.B2), weight_decay=0, eps=self.adam_eps)
    # LR scheduling, left here for forward compatibility
    # self.lr_sched = {'itr' : 0}# if self.progressive else {}
    # self.j = 0

  # Initialize
  def init_weights(self):
    self.param_count = 0
    for module in self.modules():
      if (isinstance(module, nn.Conv2d)
          or isinstance(module, nn.Linear)
          or isinstance(module, nn.Embedding)
          or isinstance(module, nn.Conv3d)):
        if self.init == 'ortho':
          init.orthogonal_(module.weight)
        elif self.init == 'N02':
          init.normal_(module.weight, 0, 0.02)
        elif self.init in ['glorot', 'xavier']:
          init.xavier_uniform_(module.weight)
        else:
          print('Init style not recognized...')
        self.param_count += sum([p.data.nelement() for p in module.parameters()])
    print('Param count for Dv''s initialized parameters: %d' % self.param_count)

  def forward(self, x, y=None,tensor_writer=None, iteration=None):
    # if y.get_device() == 0:
    #   print('y in Dv',y)
    # Stick x into h for cleaner for loops without flow control
    h = x #[B,T,C,H,W]
    # if tensor_writer != None and iteration % 1000 == 0:
    #     tensor_writer.add_video('Before Downsampling', (h[-2:] + 1)/2, iteration)
    h = h.permute(0,2,1,3,4).contiguous() #[B,C,T,H,W]
    h = self.InitDownsample(h) #[B,C,T,H/2,W/2]
    # if tensor_writer != None and iteration % 1000 == 0:
    #     tensor_writer.add_video('After Downsampling', (h[-2:].permute(0,2,1,3,4) + 1)/2, iteration)
    # Loop over blocks
    for index, blocklist in enumerate(self.blocks):
      if not self.arch['3D block'][index] and index > 0 and self.arch['3D block'][index-1]:
        h = h.permute(0,2,1,3,4)#[B,T*,C*,H*,W*]
        h = h.contiguous().view(-1,*h.shape[2:]) #[BT*,C*,H*,W*]
      for block in blocklist:
        h = block(h)
    # [BT*,C*,H*,W*]
    # Apply global sum pooling as in SN-GAN
    h = torch.sum(self.activation(h), [2, 3]) # [BT*,C*]
    if self.T_into_B == False:
      h = h.contiguous().view(x.shape[0],-1,h.shape[-1]) # [B,T*,C*]
      h = h.contiguous().view(x.shape[0],-1)# [B,T*C*]
    # Get initial class-unconditional output
    out = self.linear(h) # [B,1] if T_into_B False; [BT*,1] True
    # Get projection of final featureset onto class vectors and add to evidence
    # print('y.shape', y.shape) #28
    # print('embed(y) shape', self.embed(y).shape)
    # print('h shape', h.shape)
    # repetition = 1
    # if self.T_into_B == True:
    #   # repetition = int(h.shape[0]/x.shape[0]) #T*
    #   y = y.unsqueeze(1).repeat(1,self.reduced_t_dim,1) #[B,T*,120]
    #   # print(y.shape)
    #   y = y.contiguous().view(-1,*y.shape[2:]) #[BT*,120]
    # print('Shapes:',out.shape, self.embed(y).shape, h.shape)
    out = out + torch.sum(self.embed(y) * h, 1, keepdim=True)
    if self.T_into_B:
      return out, self.reduced_t_dim
    else:
      return out, 1


# Parallelized G_D to minimize cross-gpu communication
# Without this, Generator outputs would get all-gathered and then rebroadcast.
class G_D(nn.Module):
  def __init__(self, G, D, Dv = None, k=8, T_into_B=False):
    super(G_D, self).__init__()
    self.G = G
    self.D = D
    self.Dv = Dv
    self.k = k
    self.T_into_B = T_into_B
    # print('self.k',self.k)
  def forward(self, z, gy, x=None, dy=None, train_G=False, return_G_z=False,
              split_D=False, tensor_writer = None, iteration = None):
    # print('z shape in GD before with:',z.shape)
    # If training G, enable grad tape
    with torch.set_grad_enabled(train_G):
      # Get Generator output given noise
      # print('Entering G in GD')
      # print('z shape in GD:',z.shape)
      # print('G.shared(gy) shape:',self.G.shared(gy).shape)
      G_z = self.G(z, self.G.shared(gy)) #xiaodan: G_z:[B,T,C,H,W]
      # if G_z.get_device() == 0:
      #   print('G_z in G_D forward, B=0',G_z[0,:,0,0,0],G_z.get_device())
      #   print('gy in G_D forward',gy,gy.get_device() )
      # print('Left G in GD')
      # Cast as necessary
      if self.G.fp16 and not self.D.fp16:
        G_z = G_z.float()
      if self.D.fp16 and not self.G.fp16:
        G_z = G_z.half()
    #xiaodan: need to sample for k frames
    # print('gy,dy',gy.shape,dy.shape)
    import utils
    if self.k > 1:
      sampled_G_z,sampled_gy = utils.sample_frames(G_z,gy,self.k) # [B,8,C,H,W], [B,8]
      # if sampled_G_z.get_device() == 0:
      #   print('sampled G_z in G_D forward shape',sampled_G_z.shape,sampled_G_z.get_device())
      #   print('sampled gy in G_D forward shape',sampled_gy.shape,sampled_gy.get_device())
      sampled_G_z = sampled_G_z.contiguous().view(-1,*G_z.shape[2:])# [B*8,C,H,W]
      sampled_gy = sampled_gy.contiguous().view(-1) # [B*8]
      # if sampled_G_z.get_device() == 0:
      #   print('sampled G_z in G_D forward, B=0~1',sampled_G_z[:16,0,0,0],sampled_G_z.get_device())
      #   print('After',sampled_G_z.shape,sampled_gy.shape)
      #   print('sampled gy in G_D forward (first 16 values)',sampled_gy[:16],sampled_gy.get_device())
      # print('sampled_gy',sampled_gy.shape)
    else:
      sampled_G_z, sampled_gy = G_z.squeeze(), gy
    if x is not None and dy is not None:
      # print('x and dy shape',x.shape,dy.shape)
      if self.k > 1:
        sampled_x, sampled_dy = utils.sample_frames(x,dy,self.k) # [B,8,C,H,W], [B,8]
        sampled_x = sampled_x.contiguous().view(-1,*x.shape[2:])# [B*8,C,H,W]
        sampled_dy = sampled_dy.contiguous().view(-1,*dy.shape[2:]) # [B*8]
      else:
        sampled_x, sampled_dy = x.squeeze(), dy
    if self.Dv != None:
      if self.T_into_B:
        duplicated_gy = utils.duplicate_y(gy,self.Dv.reduced_t_dim).contiguous().view(-1,*gy.shape[2:]) # [B*3]
        if dy is not None:
          duplicated_dy = utils.duplicate_y(dy,self.Dv.reduced_t_dim).contiguous().view(-1,*dy.shape[2:]) # [B*3]
      else:
        duplicated_gy = gy
        if dy is not None:
          duplicated_dy = dy
    # print('duplicated_gy and duplicated_dy',duplicated_gy.shape,duplicated_dy.shape)


    # Split_D means to run D once with real data and once with fake,
    # rather than concatenating along the batch dimension.
    if split_D:
      D_fake = self.D(sampled_G_z, sampled_gy)
      if self.Dv != None:
        Dv_fake, repetition = self.Dv(G_z, duplicated_gy)
      if x is not None:
        D_real = self.D(sampled_x, sampled_dy)
        if self.Dv != None:
          Dv_real, repetition = self.Dv(x, duplicated_dy, tensor_writer=tensor_writer, iteration=iteration)
          return D_fake, D_real, Dv_fake, Dv_real, G_z
        else:
          return D_fake, D_real, G_z
      else:
        if return_G_z:
          if self.Dv != None:
            return D_fake, sampled_G_z, Dv_fake, G_z
          else:
            return D_fake, sampled_G_z, G_z
        else:
          if self.Dv != None:
            return D_fake, Dv_fake, G_z
          return D_fake, G_z
    # If real data is provided, concatenate it with the Generator's output
    # along the batch dimension for improved efficiency.
    else:
      D_input = torch.cat([sampled_G_z, sampled_x], 0) if x is not None else sampled_G_z
      D_class = torch.cat([sampled_gy, sampled_dy], 0) if dy is not None else sampled_gy
      Dv_input = torch.cat([G_z, x], 0) if x is not None else G_z
      Dv_class = torch.cat([duplicated_gy, duplicated_dy], 0) if dy is not None else duplicated_gy
      # print('duplicated gy dy', duplicated_gy.shape,duplicated_dy.shape)
      # Get Discriminator output
      D_out = self.D(D_input, D_class)
      if self.Dv != None:
        # print('Entering line 890')
        Dv_out, repetition = self.Dv(Dv_input, Dv_class, tensor_writer=tensor_writer, iteration=iteration)
        # print('Left line 890')
      if x is not None:
        if self.Dv != None:
          # print('repetition,Dv_out,G_z,x',repetition,Dv_out.shape,G_z.shape,x.shape)
          D_out_fake, D_out_real, Dv_out_fake, Dv_out_real = list(torch.split(D_out, [sampled_G_z.shape[0], sampled_x.shape[0]])) + list(torch.split(Dv_out, [repetition*G_z.shape[0], repetition*x.shape[0]])) # D_fake, D_real
          # print('Shapes:',D_out_fake.shape, D_out_real.shape, Dv_out_fake.shape, Dv_out_real.shape, G_z.shape)
          return D_out_fake, D_out_real, Dv_out_fake, Dv_out_real, G_z
        else:
          D_out_fake, D_out_real = list(torch.split(D_out, [sampled_G_z.shape[0], sampled_x.shape[0]]))  # D_fake, D_real
          return D_out_fake, D_out_real, G_z
      else:
        if return_G_z:
          if self.Dv != None:
            return D_out, sampled_G_z, Dv_out, G_z
          else:
            return D_out, sampled_G_z, G_z
        else:
          if self.Dv != None:
            return D_out, Dv_out, G_z
          else:
            return D_out, G_z
