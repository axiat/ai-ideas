import torch
import torch.nn as nn
import torch.nn.functional as F
from ptflops import get_model_complexity_info
import basicblock as B
from utils_image import *
from utils import *
import time
from math import ceil
import cv2
from timm.models.layers import DropPath
from SwinTransformers import SwinTransformer  # models.


def kaiming_init(module):
    if isinstance(module, nn.Conv2d):
        nn.init.kaiming_normal_(module.weight.data)
        if module.bias is not None:
            nn.init.constant_(module.bias.data, 0)
    elif isinstance(module, nn.Linear):
        nn.init.kaiming_normal_(module.weight.data)
        if module.bias is not None:
            nn.init.constant_(module.bias.data, 0)


def conv3x3_bn_relu(in_planes, out_planes, k=3, s=1, p=1, b=False):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=k, stride=s, padding=p, bias=b),
        nn.BatchNorm2d(out_planes),
        nn.GELU(),
    )


class HeadNet(nn.Module):
    def __init__(self, in_nc, nc_x, out_nc, d_size):
        super(HeadNet, self).__init__()
        self.head_zp = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_ax = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_ay = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_cp = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_vx = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_vy = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_vp = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_ux = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_uy = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.head_um = nn.Sequential(nn.Conv2d(in_nc * 6, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
                                     nn.BatchNorm2d(nc_x[0]),
                                     nn.ReLU(inplace=True),
                                     nn.Conv2d(nc_x[0], out_nc, d_size, padding=(d_size - 1) // 2, bias=False))
        self.apply(kaiming_init)

    def forward(self, x, y, x3, y3):
        xm, xp = amp_pha(x)  # 获得幅度谱,相位谱
        ym, yp = amp_pha(y)
        out_xp = IFFT_xp(xp)  # 相位谱逆变换
        out_yp = IFFT_xp(yp)
        zp = self.head_zp(torch.cat([x3, y3], dim=1))
        ax = self.head_ax(torch.cat([x3, y3], dim=1))
        ay = self.head_ay(torch.cat([x3, y3], dim=1))
        cp = self.head_cp(torch.cat([x3, y3], dim=1))
        vx = self.head_vx(torch.cat([x3, y3], dim=1))
        vy = self.head_vy(torch.cat([x3, y3], dim=1))
        vp = self.head_vp(torch.cat([x3, y3], dim=1))
        ux = self.head_ux(torch.cat([x3, y3], dim=1))
        uy = self.head_uy(torch.cat([x3, y3], dim=1))
        um = self.head_um(torch.cat([x3, y3], dim=1))
        return out_xp, out_yp, zp, ax, ay, cp, vx, vy, vp, ux, uy, um


class HyPaNet(nn.Module):
    def __init__(self, in_nc: int = 1, nc: int = 256, out_nc: int = 1, ):
        super(HyPaNet, self).__init__()
        self.mlp = nn.Sequential(
            nn.Conv2d(in_nc, nc, 1, padding=0, bias=True), nn.Sigmoid(),
            nn.Conv2d(nc, out_nc, 1, padding=0, bias=True), nn.Softplus())
        self.apply(kaiming_init)

    def forward(self, out_xp, out_yp, out_zp):  #
        x = torch.cat([out_xp, out_yp, out_zp], dim=1)  # torch.cat([out_xp, out_yp], dim=1), out_zp
        x = (x - 0.098) / 0.0566
        x = self.mlp(x) + 1e-6
        return x


class Update_av(nn.Module):
    def __init__(self):
        super(Update_av, self).__init__()

    def forward(self, z, x, v, lam):
        out_a = z - x + v
        out_a = torch.mul(torch.sign(out_a), F.relu(torch.abs(out_a) - lam))
        out_v = v + z - x - out_a
        return out_a, out_v


class Update_m(nn.Module):
    def __init__(self,
                 in_nc=1,
                 nc_x=[64, 128, 256, 512],
                 nb=4):
        super(Update_m, self).__init__()
        self.encode = nn.Sequential(
            B.conv(in_nc * 2, nc_x[0], bias=False, mode='C'),
            B.conv(nc_x[0], nc_x[0], bias=False, mode='R'),
            B.conv(nc_x[0], nc_x[0], bias=False, mode='C'), )
        self.m_down1 = B.sequential(
            *[
                B.ResBlock(nc_x[0], nc_x[0], bias=False, mode='CRC')
                for _ in range(nb)
            ], B.downsample_strideconv(nc_x[0], nc_x[1], bias=False, mode='2'))
        self.m_down2 = B.sequential(
            *[
                B.ResBlock(nc_x[1], nc_x[1], bias=False, mode='CRC')
                for _ in range(nb)
            ], B.downsample_strideconv(nc_x[1], nc_x[2], bias=False, mode='2'))
        self.m_down3 = B.sequential(
            *[
                B.ResBlock(nc_x[2], nc_x[2], bias=False, mode='CRC')
                for _ in range(nb)
            ], B.downsample_strideconv(nc_x[2], nc_x[3], bias=False, mode='2'))

        self.m_body = B.sequential(*[
            B.ResBlock(nc_x[-1], nc_x[-1], bias=False, mode='CRC')
            for _ in range(nb)])

        self.m_up3 = B.sequential(
            B.upsample_convtranspose(nc_x[3], nc_x[2], bias=False, mode='2'),
            *[B.ResBlock(nc_x[2], nc_x[2], bias=False, mode='CRC')
              for _ in range(nb)])
        self.m_up2 = B.sequential(
            B.upsample_convtranspose(nc_x[2], nc_x[1], bias=False, mode='2'),
            *[B.ResBlock(nc_x[1], nc_x[1], bias=False, mode='CRC')
              for _ in range(nb)])
        self.m_up1 = B.sequential(
            B.upsample_convtranspose(nc_x[1], nc_x[0], bias=False, mode='2'),
            *[B.ResBlock(nc_x[0], nc_x[0], bias=False, mode='CRC')
              for _ in range(nb)])

        self.m_tail = B.conv(nc_x[0], in_nc, bias=False, mode='C')
        self.apply(kaiming_init)

    def forward(self, x, gamma):
        gamma = reshape_params4(gamma, x)
        x1 = self.encode(torch.cat([x, gamma], dim=1))
        x2 = self.m_down1(x1)
        x3 = self.m_down2(x2)
        x4 = self.m_down3(x3)
        x = self.m_body(x4)
        x = self.m_up3(x + x4)
        x = self.m_up2(x + x3)
        x = self.m_up1(x + x2)
        x = self.m_tail(x + x1)
        return x


class SA_Enhance(nn.Module):
    def __init__(self, kernel_size=7):
        super(SA_Enhance, self).__init__()

        assert kernel_size in (3, 7), 'kernel size must be 3 or 7'
        padding = 3 if kernel_size == 7 else 1

        self.conv1 = nn.Conv2d(1, 1, kernel_size, padding=padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        x = max_out
        x = self.conv1(x)
        return self.sigmoid(x)


class h_sigmoid(nn.Module):
    def __init__(self, inplace=True):
        super(h_sigmoid, self).__init__()
        self.relu = nn.ReLU6(inplace=inplace)

    def forward(self, x):
        return self.relu(x + 3) / 6


class h_swish(nn.Module):
    def __init__(self, inplace=True):
        super(h_swish, self).__init__()
        self.sigmoid = h_sigmoid(inplace=inplace)

    def forward(self, x):
        return x * self.sigmoid(x)

class MaskGenerator(nn.Module):
    def __init__(self, in_channels,min_channels, out_channels):
        super(MaskGenerator, self).__init__()
        self.mlp = nn.Sequential(
            nn.Conv2d(in_channels, min_channels, kernel_size=3, padding=1, bias=True), nn.Sigmoid(),
            nn.Conv2d(min_channels, out_channels, kernel_size=3, padding=1, bias=True), nn.Softplus())
    def forward(self, rgb, depth, phase):  #
        x = torch.cat([rgb, depth, phase], dim=1)
        x = (x - 0.098) / 0.0566
        x = self.mlp(x) + 1e-6
        return x
class MultiScaleMaskGenerator(nn.Module):
    def __init__(self, in_channels, min_channels, out_channels, num_scales=3):
        super(MultiScaleMaskGenerator, self).__init__()
        self.num_scales = num_scales
        self.scale_modules = nn.ModuleList()
        for i in range(num_scales):
            # 不同尺度的卷积层组合
            scale_module = nn.Sequential(
                nn.Conv2d(in_channels, min_channels, kernel_size=3 + 2 * i, padding=1 + i, bias=True),
                nn.BatchNorm2d(min_channels),
                nn.GELU(),
                nn.Conv2d(min_channels, out_channels, kernel_size=3 + 2 * i, padding=1 + i, bias=True),
                nn.Sigmoid()
            )
            self.scale_modules.append(scale_module)
    def forward(self, rgb, depth, phase):
        x = torch.cat([rgb, depth, phase], dim=1)
        mask = 0
        for scale_module in self.scale_modules:
            mask += scale_module(x)
        return mask
class CoordAtt(nn.Module):
    def __init__(self, inp, oup, large_kernel, reduction=32):
        super(CoordAtt, self).__init__()
        self.inp = inp
        self.pool_h = nn.AdaptiveAvgPool2d((None, 1))
        self.pool_w = nn.AdaptiveAvgPool2d((1, None))

        mip = max(8, inp // reduction)

        self.conv1 = nn.Conv2d(inp, mip, kernel_size=1, stride=1, padding=0)
        self.bn1 = nn.BatchNorm2d(mip)
        self.act = h_swish()

        self.conv_h = nn.Conv2d(mip, oup, kernel_size=1, stride=1, padding=0)
        self.conv_w = nn.Conv2d(mip, oup, kernel_size=1, stride=1, padding=0)
        self.conv_end = nn.Conv2d(oup, oup // 3, kernel_size=1, stride=1, padding=0)
        # self.conv_end = RepLKBlocks2(oup, oup// 3, block_lk_size=large_kernel, mid_kernel=5, small_kernel=3, drop_path=0)
        self.self_SA_Enhance = SA_Enhance()

        # 交叉注意力的线性变换层
        self.mask_generator = MultiScaleMaskGenerator(in_channels=inp, min_channels=inp// 3, out_channels=3,num_scales=3)

    def forward(self, rgb, depth, phase):
        # x = torch.cat((rgb, depth, phase), dim=1)
        mask = self.mask_generator(rgb, depth, phase)
        mask_rgb, mask_depth, mask_phase = mask[:, 0:1], mask[:, 1:2], mask[:, 2:3]
        # mask_rgb, mask_depth, mask_phase = mask[:, 0:self.inp//3], mask[:, self.inp//3:2 * self.inp//3], mask[:, 2 * self.inp//3:self.inp]
        weighted_rgb = mask_rgb * rgb
        weighted_depth = mask_depth * depth
        weighted_phase = mask_phase * phase
        x = torch.cat((weighted_rgb, weighted_depth, weighted_phase), dim=1)

        n, c, h, w = x.size()
        x_h = self.pool_h(x)
        x_w = self.pool_w(x).permute(0, 1, 3, 2)

        y = torch.cat([x_h, x_w], dim=2)#torch.Size([10, 3072, 24, 1])
        y = self.conv1(y)
        y = self.bn1(y)
        y = self.act(y)

        x_h, x_w = torch.split(y, [h, w], dim=2)
        x_w = x_w.permute(0, 1, 3, 2)

        a_h = self.conv_h(x_h).sigmoid()
        a_w = self.conv_w(x_w).sigmoid()

        out_ca = x * a_w * a_h #torch.Size([10, 3072, 12, 12])
        out_sa = self.self_SA_Enhance(out_ca) #out2 torch.Size([10, 1, 12, 12])
        out = x.mul(out_sa)#torch.Size([10, 3072, 12, 12])
        out = self.conv_end(out)

        return out, mask_rgb, mask_depth, mask_phase


def drop_path(x, drop_prob: float = 0., training: bool = False):
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()  # binarize
    output = x.div(keep_prob) * random_tensor
    return output


class DropPath(nn.Module):
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class LayerNorm(nn.Module):
    def __init__(self, normalized_shape, eps=1e-6, data_format="channels_first"):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(normalized_shape), requires_grad=True)
        self.bias = nn.Parameter(torch.zeros(normalized_shape), requires_grad=True)
        self.eps = eps
        self.data_format = data_format
        if self.data_format not in ["channels_last", "channels_first"]:
            raise ValueError(f"not support data format '{self.data_format}'")
        self.normalized_shape = (normalized_shape,)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.data_format == "channels_last":
            return F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        elif self.data_format == "channels_first":
            # [batch_size, channels, height, width]
            mean = x.mean(1, keepdim=True)
            var = (x - mean).pow(2).mean(1, keepdim=True)
            x = (x - mean) / torch.sqrt(var + self.eps)
            x = self.weight[:, None, None] * x + self.bias[:, None, None]
            return x


class Block(nn.Module):
    def __init__(self, dim, drop_rate=0., layer_scale_init_value=1e-6):
        super().__init__()
        self.dwconv = nn.Conv2d(dim, dim, kernel_size=7, padding=3, groups=dim)  # depthwise conv
        self.norm = LayerNorm(dim, eps=1e-6, data_format="channels_last")
        self.pwconv1 = nn.Linear(dim, 4 * dim)  # pointwise/1x1 convs, implemented with linear layers
        self.act = nn.GELU()
        self.pwconv2 = nn.Linear(4 * dim, dim)
        self.gamma = nn.Parameter(layer_scale_init_value * torch.ones((dim,)), requires_grad=True) if layer_scale_init_value > 0 else None
        self.drop_path = DropPath(drop_rate) if drop_rate > 0. else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shortcut = x
        x = self.dwconv(x)
        x = x.permute(0, 2, 3, 1)  # [N, C, H, W] -> [N, H, W, C]
        x = self.norm(x)
        x = self.pwconv1(x)
        x = self.act(x)
        x = self.pwconv2(x)
        if self.gamma is not None:
            x = self.gamma * x
        x = x.permute(0, 3, 1, 2)  # [N, H, W, C] -> [N, C, H, W]

        x = shortcut + self.drop_path(x)
        return x

class ReparamLargeKernelConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, groups, mid_kernel, small_kernel):
        super(ReparamLargeKernelConv, self).__init__()
        self.kernel_size = kernel_size
        self.small_kernel = small_kernel
        padding = kernel_size // 2
        self.lkb_origin = nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=padding, stride=stride, bias=False, groups=groups), nn.BatchNorm2d(out_channels))
        self.small_conv = nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size=mid_kernel, padding=mid_kernel//2, stride=stride, bias=False, groups=groups), nn.BatchNorm2d(out_channels))
        self.tiny_conv = nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size=small_kernel, padding=small_kernel//2, stride=stride, bias=False, groups=groups), nn.BatchNorm2d(out_channels))
    def forward(self, inputs):
        out = self.lkb_origin(inputs)
        out += self.small_conv(inputs)
        out += self.tiny_conv(inputs)
        return out
class RepLKBlock(nn.Module):
    def __init__(self, in_channels, out_channels, block_lk_size, mid_kernel, small_kernel, drop_path):
        super().__init__()
        self.pw1 = nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size=1, padding=0, stride=1, bias=False, groups=1), nn.BatchNorm2d(out_channels), nn.ReLU(inplace=True))
        self.pw2 = nn.Sequential(nn.Conv2d(out_channels, in_channels, kernel_size=1, padding=0, stride=1, bias=False, groups=1), nn.BatchNorm2d(in_channels))
        self.large_kernel = ReparamLargeKernelConv(in_channels=out_channels, out_channels=out_channels, kernel_size=block_lk_size, stride=1, groups=out_channels, mid_kernel=mid_kernel, small_kernel=small_kernel)
        self.lk_nonlinear = nn.ReLU()
        self.prelkb_bn = nn.BatchNorm2d(in_channels)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
    def forward(self, x):
        out = self.prelkb_bn(x)
        out = self.pw1(out)
        out = self.large_kernel(out)
        out = self.lk_nonlinear(out)
        out = self.pw2(out)
        return out + self.drop_path(out)
class RepLKBlocks(nn.Module):
    def __init__(self, out_channels, block_lk_size=[31,31], mid_kernel=5, small_kernel=3, drop_path=0.0):
        super().__init__()
        self.num_blocks = len(block_lk_size)
        self.lk_blocks: nn.ModuleList = nn.ModuleList()
        for i in range(self.num_blocks):
            self.lk_blocks.append(RepLKBlock(out_channels, out_channels, block_lk_size[i], mid_kernel, small_kernel, drop_path))
    def forward(self, x):
        for i in range(self.num_blocks):
            x = self.lk_blocks[i](x)
        return x
class RepLKBlocks2(nn.Module):
    def __init__(self, in_channels, out_channels, block_lk_size=[31,31], mid_kernel=5, small_kernel=3, drop_path=0.0):
        super().__init__()
        self.num_blocks = len(block_lk_size)
        self.lk_blocks: nn.ModuleList = nn.ModuleList()
        self.in_fuse = conv3x3_bn_relu(in_channels, out_channels, k=3, s=1, p=1, b=False)
        for i in range(self.num_blocks):
            self.lk_blocks.append(RepLKBlock(out_channels, out_channels, block_lk_size[i], mid_kernel, small_kernel, drop_path))
        # self.out_fuse = conv3x3_bn_relu(out_channels, out_channels, k=3, s=1, p=1, b=False)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
    def forward(self, x):
        x = self.in_fuse(x)
        for i in range(self.num_blocks):
            x = self.lk_blocks[i](x)
        return x
class CPNet(nn.Module):
    def __init__(self):
        super(CPNet, self).__init__()

        self.x_swin = SwinTransformer(embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32])
        self.y_swin = SwinTransformer(embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32])
        self.pz_swin = SwinTransformer(embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32])
        self.up2 = nn.UpsamplingBilinear2d(scale_factor=2)
        self.up4 = nn.UpsamplingBilinear2d(scale_factor=4)

        self.CA_SA_Enhance_1 = CoordAtt(1024 * 3, 1024 * 3, [11,11])
        self.CA_SA_Enhance_2 = CoordAtt(512 * 3, 512 * 3, [23,23])
        self.CA_SA_Enhance_3 = CoordAtt(256 * 3, 256 * 3, [31,31])
        self.CA_SA_Enhance_4 = CoordAtt(128 * 3, 128 * 3, [31,31])

        # self.FA_Block2 = Block(dim=256)
        # self.FA_Block3 = Block(dim=128)
        # self.FA_Block4 = Block(dim=64)
        self.LK_Block2 = RepLKBlocks(256)
        self.LK_Block3 = RepLKBlocks(128)
        self.LK_Block4 = RepLKBlocks(64)

        self.upsample2 = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        self.deconv_layer_1 = nn.Sequential(
            nn.Conv2d(in_channels=1024, out_channels=512, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(512),
            nn.GELU(),
            self.upsample2
        )
        self.deconv_layer_2 = nn.Sequential(
            nn.Conv2d(in_channels=1024, out_channels=256, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(256),
            nn.GELU(),
            self.upsample2
        )
        self.deconv_layer_3 = nn.Sequential(
            nn.Conv2d(in_channels=512, out_channels=128, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(128),
            nn.GELU(),
            self.upsample2
        )
        self.deconv_layer_4 = nn.Sequential(
            nn.Conv2d(in_channels=256, out_channels=64, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.GELU(),
            self.upsample2
        )
        self.predict_layer_1 = nn.Sequential(
            nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.GELU(),
            self.upsample2,
            nn.Conv2d(in_channels=32, out_channels=1, kernel_size=3, padding=1, bias=True),
        )
        self.predtrans2 = nn.Conv2d(128, 1, kernel_size=3, padding=1)
        self.predtrans3 = nn.Conv2d(256, 1, kernel_size=3, padding=1)
        self.predtrans4 = nn.Conv2d(512, 1, kernel_size=3, padding=1)
        self.dwc3 = conv3x3_bn_relu(256, 128)
        self.dwc2 = conv3x3_bn_relu(512, 256)
        self.dwc1 = conv3x3_bn_relu(1024, 512)
        self.dwcon_1 = conv3x3_bn_relu(2048, 1024)
        self.dwcon_2 = conv3x3_bn_relu(1024, 512)
        self.dwcon_3 = conv3x3_bn_relu(512, 256)
        self.dwcon_4 = conv3x3_bn_relu(256, 128)
        self.conv43 = conv3x3_bn_relu(128, 256, s=2)
        self.conv32 = conv3x3_bn_relu(256, 512, s=2)
        self.conv21 = conv3x3_bn_relu(512, 1024, s=2)

    def forward(self, x, y, p):
        rgb_list = self.x_swin(x)  # x torch.Size([2, 3, 384, 384])
        depth_list = self.y_swin(y)  # d torch.Size([2, 3, 384, 384])
        phase_list = self.pz_swin(p)

        r4 = rgb_list[0]  # torch.Size([2, 128, 96, 96])
        r3 = rgb_list[1]  # torch.Size([2, 256, 48, 48])
        r2 = rgb_list[2]  # torch.Size([2, 512, 24, 24])
        r1 = rgb_list[3]  # torch.Size([2, 1024, 12, 12])
        d4 = depth_list[0]  # torch.Size([2, 128, 96, 96])
        d3 = depth_list[1]  # torch.Size([2, 256, 48, 48])
        d2 = depth_list[2]  # torch.Size([2, 512, 24, 24])
        d1 = depth_list[3]  # torch.Size([2, 1024, 12, 12])
        p4 = phase_list[0]  # torch.Size([2, 128, 96, 96])
        p3 = phase_list[1]  # torch.Size([2, 256, 48, 48])
        p2 = phase_list[2]  # torch.Size([2, 512, 24, 24])
        p1 = phase_list[3]  # torch.Size([2, 1024, 12, 12])

        r3_up = F.interpolate(self.dwc3(r3), size=96, mode='bilinear')  # torch.Size([2, 128, 96, 96])
        r2_up = F.interpolate(self.dwc2(r2), size=48, mode='bilinear')  # torch.Size([2, 256, 48, 48])
        r1_up = F.interpolate(self.dwc1(r1), size=24, mode='bilinear')  # torch.Size([2, 512, 24, 24])
        d3_up = F.interpolate(self.dwc3(d3), size=96, mode='bilinear')  # torch.Size([2, 128, 96, 96])
        d2_up = F.interpolate(self.dwc2(d2), size=48, mode='bilinear')  # torch.Size([2, 256, 48, 48])
        d1_up = F.interpolate(self.dwc1(d1), size=24, mode='bilinear')  # torch.Size([2, 512, 24, 24])
        p3_up = F.interpolate(self.dwc3(p3), size=96, mode='bilinear')  # torch.Size([2, 128, 96, 96])
        p2_up = F.interpolate(self.dwc2(p2), size=48, mode='bilinear')  # torch.Size([2, 256, 48, 48])
        p1_up = F.interpolate(self.dwc1(p1), size=24, mode='bilinear')  # torch.Size([2, 512, 24, 24])

        r1_con = torch.cat((r1, r1), 1)  # torch.Size([2, 2048, 12, 12])
        r1_con = self.dwcon_1(r1_con)  # torch.Size([2, 1024, 12, 12])
        d1_con = torch.cat((d1, d1), 1)
        d1_con = self.dwcon_1(d1_con)
        p1_con = torch.cat((p1, p1), 1)
        p1_con = self.dwcon_1(p1_con)

        r2_con = torch.cat((r2, r1_up), 1)  # torch.Size([2, 1024, 24, 24])
        r2_con = self.dwcon_2(r2_con)  # torch.Size([2, 512, 24, 24])
        d2_con = torch.cat((d2, d1_up), 1)  #
        d2_con = self.dwcon_2(d2_con)  #
        p2_con = torch.cat((p2, p1_up), 1)  #
        p2_con = self.dwcon_2(p2_con)  #

        r3_con = torch.cat((r3, r2_up), 1)  # torch.Size([2, 512, 48, 48])
        r3_con = self.dwcon_3(r3_con)  # torch.Size([2, 256, 48, 48])
        d3_con = torch.cat((d3, d2_up), 1)  #
        d3_con = self.dwcon_3(d3_con)  #
        p3_con = torch.cat((p3, p2_up), 1)  #
        p3_con = self.dwcon_3(p3_con)  #

        r4_con = torch.cat((r4, r3_up), 1)  # torch.Size([2, 256, 96, 96])
        r4_con = self.dwcon_4(r4_con)  # torch.Size([2, 128, 96, 96])
        d4_con = torch.cat((d4, d3_up), 1)  #
        d4_con = self.dwcon_4(d4_con)  #
        p4_con = torch.cat((p4, p3_up), 1)  #
        p4_con = self.dwcon_4(p4_con)  #

        xf_1, mask_rgb_1, mask_depth_1, mask_phase_1 = self.CA_SA_Enhance_1(r1_con, d1_con, p1_con)  # torch.Size([2, 1024, 12, 12])
        xf_2, mask_rgb_2, mask_depth_2, mask_phase_2 = self.CA_SA_Enhance_2(r2_con, d2_con, p2_con)  # torch.Size([2, 512, 24, 24])
        xf_3, mask_rgb_3, mask_depth_3, mask_phase_3 = self.CA_SA_Enhance_3(r3_con, d3_con, p3_con)  # torch.Size([2, 256, 48, 48])
        xf_4, mask_rgb_4, mask_depth_4, mask_phase_4 = self.CA_SA_Enhance_4(r4_con, d4_con, p4_con)  # torch.Size([2, 128, 96, 96])


        df_f_1 = self.deconv_layer_1(xf_1)#torch.Size([2, 512, 24, 24])

        xc_1_2 = torch.cat((df_f_1, xf_2), 1)
        df_f_2 = self.deconv_layer_2(xc_1_2)#torch.Size([1, 256, 48, 48])
        # df_f_2 = self.FA_Block2(df_f_2)
        df_f_2 = self.LK_Block2(df_f_2)

        xc_1_3 = torch.cat((df_f_2, xf_3), 1)
        df_f_3 = self.deconv_layer_3(xc_1_3)#torch.Size([1, 128, 96, 96])
        # df_f_3 = self.FA_Block3(df_f_3)
        df_f_3 = self.LK_Block3(df_f_3)

        xc_1_4 = torch.cat((df_f_3, xf_4), 1)
        df_f_4 = self.deconv_layer_4(xc_1_4)# torch.Size([1, 64, 192, 192])
        # df_f_4 = self.FA_Block4(df_f_4)
        df_f_4 = self.LK_Block4(df_f_4)

        y1 = self.predict_layer_1(df_f_4)
        y2 = F.interpolate(self.predtrans2(df_f_3), size=384, mode='bilinear')
        y3 = F.interpolate(self.predtrans3(df_f_2), size=384, mode='bilinear')
        y4 = F.interpolate(self.predtrans4(df_f_1), size=384, mode='bilinear')
        mask_rgb_1, mask_depth_1, mask_phase_1 = F.interpolate(mask_rgb_1, size=384, mode='bilinear'), F.interpolate(mask_depth_1, size=384, mode='bilinear'), F.interpolate(mask_phase_1, size=384, mode='bilinear')
        mask_rgb_2, mask_depth_2, mask_phase_2 = F.interpolate(mask_rgb_2, size=384, mode='bilinear'), F.interpolate(mask_depth_2, size=384, mode='bilinear'), F.interpolate(mask_phase_2, size=384, mode='bilinear')
        mask_rgb_3, mask_depth_3, mask_phase_3 = F.interpolate(mask_rgb_3, size=384, mode='bilinear'), F.interpolate(mask_depth_3, size=384, mode='bilinear'), F.interpolate(mask_phase_3, size=384, mode='bilinear')
        mask_rgb_4, mask_depth_4, mask_phase_4 = F.interpolate(mask_rgb_4, size=384, mode='bilinear'), F.interpolate(mask_depth_4, size=384, mode='bilinear'), F.interpolate(mask_phase_4, size=384, mode='bilinear')
        return y1, y2, y3, y4, mask_rgb_1, mask_depth_1, mask_phase_1, mask_rgb_2, mask_depth_2, mask_phase_2, mask_rgb_3, mask_depth_3, mask_phase_3, mask_rgb_4, mask_depth_4, mask_phase_4



class ScoreLayer(nn.Module):
    def __init__(self, k=32):
        super(ScoreLayer, self).__init__()
        self.score = nn.Conv2d(k, 1, 1, 1)

    def forward(self, x):
        x = self.score(x)
        return x


class ParamModule(nn.Module):
    def __init__(self, num):
        super().__init__()
        self.param = nn.Parameter(num * torch.ones(1, 1, 1, 1), requires_grad=True)

    def forward(self):
        return self.param


class Stage_p(nn.Module):
    # for phase consistency, input is out_xp, out_yp
    def __init__(self, in_nc=1, nc_x=[64, 128, 256, 512], nb=4, is_last=False):
        super(Stage_p, self).__init__()
        self.is_last = is_last
        if self.is_last:
            pass
        else:
            self.up_cp = Update_m(in_nc=in_nc, nc_x=nc_x, nb=nb)
            self.up_avx = Update_av()
            self.up_avy = Update_av()

        self.apply(kaiming_init)

    def forward(self, x, y, ax, ay, cp, vx, vy, vp, u1, u2, lambda1=0, lambda2=0, beta1=0, beta2=0, beta3=0, gamma1=0):
        # x + ux
        x = x + u1 * torch.ones_like(x)
        y = y + u2 * torch.ones_like(y)
        # update zp
        zp = (1 / (beta1 + beta2 + beta3 + 1e-8)) * (beta1 * (x + ax - vx) + beta2 * (y + ay - vy) + beta3 * (cp - vp))
        if self.is_last:
            return zp
        else:
            # update a and v input: z,x,v,lam
            ax, vx = self.up_avx(zp, x, vx, lam=lambda1 / (beta1 + 1e-8))
            ay, vy = self.up_avy(zp, y, vy, lam=lambda2 / (beta2 + 1e-8))
            # update cp and vp input: x, gamma
            cp = self.up_cp(zp + vp, beta3 / (gamma1 + 1e-8))
            vp = vp + zp - cp
            return zp, ax, ay, cp, vx, vy, vp


class Stage_z(nn.Module):
    def __init__(self, in_nc=1, nc_x=[64, 128, 256, 512], nb=4):
        super(Stage_z, self).__init__()
        self.up_zx = Update_m(in_nc=in_nc, nc_x=nc_x, nb=nb)
        self.up_zy = Update_m(in_nc=in_nc, nc_x=nc_x, nb=nb)
        self.up_m = Update_m(in_nc=in_nc, nc_x=nc_x, nb=nb)
        self.apply(kaiming_init)

    def forward(self, x, y, z, ux, uy, um, deta1=0, deta2=0, epsilon1=0, epsilon2=0, epsilon3=0, gamma3=0):
        # update zx, ux
        zx = self.up_zx(z - x + ux, epsilon1 / (deta1 + 1e-8))
        ux = ux + z - x - zx
        # update zy, uy
        zy = self.up_zy(z - y + uy, epsilon2 / (deta2 + 1e-8))
        uy = uy + z - y - zy
        # update m, um
        m = self.up_m(z + um, epsilon3 / (gamma3 + 1e-8))
        um = um + z - m
        # update z
        z = (1 / (epsilon1 + epsilon2 + epsilon3 + 1e-8)) * (epsilon1 * (x + zx - ux) + epsilon2 * (y + zy - uy) + epsilon3 * (m - um))
        return z, zx, ux, zy, uy, m, um


class PUTNet(nn.Module):
    def __init__(self, in_channels=1, out_channels=1, num_of_layers_stage1=2, num_of_layers_stage2=2, nc_x=[64, 128, 256, 512], nb=4, d_size=3):
        super(PUTNet, self).__init__()
        self.num_of_layers_stage1 = num_of_layers_stage1
        self.num_of_layers_stage2 = num_of_layers_stage2
        self.head = HeadNet(in_nc=in_channels, nc_x=nc_x, out_nc=out_channels, d_size=d_size)
        self.initial_z = CPNet()
        self.hypa_list_p: nn.ModuleList = nn.ModuleList()
        self.update_p: nn.ModuleList = nn.ModuleList()
        for i in range(num_of_layers_stage1):
            if i == self.num_of_layers_stage1 - 1:
                is_last = True
            else:
                is_last = False
            self.hypa_list_p.append(HyPaNet(in_nc=3, out_nc=1 * 8))
            self.update_p.append(Stage_p(in_nc=in_channels, nc_x=nc_x, nb=nb, is_last=is_last))

    def forward(self, x,x3,y,y3):  # x,x3,y,y3
        # c = xy.shape[1]
        # x = xy[:, :1, :, :]
        # x3 = xy[:, 1:4, :, :]
        # y = xy[:, 4:5, :, :]
        # y3 = xy[:, 5:8, :, :]

        preds = []
        Px, Py, Pz, ax, ay, cp, vx, vy, vp, ux, uy, um = self.head(x, y, x3, y3)
        preds.append(Pz)
        for i in range(self.num_of_layers_stage1):
            hypas_p = self.hypa_list_p[i](x, y, Pz)
            lambda1 = hypas_p[:, 0:1]
            lambda2 = hypas_p[:, 1:2]
            beta1 = hypas_p[:, 2:3]
            beta2 = hypas_p[:, 3:4]
            beta3 = hypas_p[:, 4:5]
            gamma1 = hypas_p[:, 5:6]
            u1 = hypas_p[:, 6:7]  # mean for x-Pz
            u2 = hypas_p[:, 7:8]  # mean for x-Pz
            if i == self.num_of_layers_stage1 - 1:
                Pz = self.update_p[i](Px, Py, ax, ay, cp, vx, vy, vp, u1, u2, lambda1, lambda2, beta1, beta2, beta3,gamma1)
            else:
                Pz, ax, ay, cp, vx, vy, vp = self.update_p[i](Px, Py, ax, ay, cp, vx, vy, vp, u1, u2, lambda1, lambda2,beta1, beta2, beta3, gamma1)
        z, z2, z3, z4, mask_rgb_1, mask_depth_1, mask_phase_1, mask_rgb_2, mask_depth_2, mask_phase_2, mask_rgb_3, mask_depth_3, mask_phase_3, mask_rgb_4, mask_depth_4, mask_phase_4 = self.initial_z(x3, y3, Pz.repeat(1, 3, 1, 1))
        preds.append(z4)
        preds.append(z3)
        preds.append(z2)
        preds.append(z)
        return preds,[mask_rgb_1, mask_depth_1, mask_phase_1, mask_rgb_2, mask_depth_2, mask_phase_2, mask_rgb_3, mask_depth_3, mask_phase_3, mask_rgb_4, mask_depth_4, mask_phase_4]


def data_process_npy(data_in):
    data_out = data_in.detach().float().cpu().numpy()
    data_out = np.transpose(data_out, (0, 2, 3, 1))
    data_out = data_out.squeeze()
    # data_out = normalize_to_minus1_1_max(data_out)
    return data_out  # *255


if __name__ == '__main__':
    net = FCNet()#.cuda()
    print(net)
    xy = torch.rand([1, 8, 384, 384])#.cuda()
    a_out,_ = net(xy)
    for i in range(len(a_out)):
        print(a_out[i].shape)
    macs, params = get_model_complexity_info(net, (8, 384, 384), as_strings=True, print_per_layer_stat=True, verbose=True)
    print('{:<30}  {:<8}'.format('Computational complexity: ', macs))
    print('{:<30}  {:<8}'.format('Number of parameters: ', params))
