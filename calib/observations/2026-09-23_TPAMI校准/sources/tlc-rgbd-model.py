import torch
import torch.nn as nn
import torch.nn.functional as F
from ptflops import get_model_complexity_info
import basicblock as B
from utils_image import *
from utils import *
import time
from math import ceil

class HeadNet(nn.Module):
    def __init__(self, in_nc, nc_x, out_nc, d_size):
        super(HeadNet, self).__init__()
        self.head_z = nn.Sequential(
            nn.Conv2d(in_nc * 2,nc_x[0],d_size,padding=(d_size - 1) // 2,bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_m = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_u = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_n = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_b = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_v = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
        self.head_w = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_x[0], d_size, padding=(d_size - 1) // 2, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_x[0], out_nc, d_size, padding=1, bias=False))
    def forward(self, x,y):
        z = self.head_z(torch.cat([x, y], dim=1))
        m = self.head_m(torch.cat([x, y], dim=1))
        n = self.head_n(torch.cat([x, y], dim=1))
        b = self.head_b(torch.cat([x, y], dim=1))
        v = self.head_v(torch.cat([x, y], dim=1))
        u = self.head_u(torch.cat([x, y], dim=1))
        w = self.head_w(torch.cat([x, y], dim=1))
        return z,m,n,b,v,u,w
class HyPaNet(nn.Module):
    def __init__(self,in_nc: int = 1,nc: int = 256,out_nc: int = 3,):
        super(HyPaNet, self).__init__()
        self.mlp = nn.Sequential(
            nn.Conv2d(in_nc*2, nc, 1, padding=0, bias=True), nn.Sigmoid(),
            nn.Conv2d(nc, out_nc, 1, padding=1, bias=True), nn.Softplus())
    def forward(self, x, y):
        x = torch.cat([x,y], dim=1)
        x = (x - 0.098) / 0.0566
        x = self.mlp(x) + 1e-6
        return x
class Update_m(nn.Module):
    def __init__(self):
        super(Update_m, self).__init__()
    def forward(self, z, u, n, w, H, x, lambda1=1, eta2=1, eta3=1):
        lambda1 = reshape_params4(lambda1,z)
        eta2 = reshape_params4(eta2,z)
        eta3 = reshape_params4(eta3,z)

        l1 = eta2/lambda1
        l2 = eta3/lambda1
        ler = l1+l2

        _H = cconj(H)
        _M = H * x + l1 * (z+u) + l2 * (n-w)
        factor1 = _M / ler
        numerator = _H * _M
        denominator = ler * _H * H + ler.squeeze(-1)**2
        factor2 = H * torch.div(numerator, denominator)
        M = (factor1 - factor2)
        return M
def laplace_weight(N,C_in,C_out):
    kernelx = [[-1, -1, -1],
                [-1, 8 , -1],
                [-1, -1, -1]]
    kernelx = torch.FloatTensor(kernelx).expand(N,C_out,C_in,3,3)
    weightx = nn.Parameter(data=kernelx, requires_grad=False).cuda()
    return weightx
class Update_n(nn.Module):
    def __init__(self):
        super(Update_n, self).__init__()
    def forward(self, n,a,b,v,m,w,eta1=1,eta3=1):
        N = n.shape[0]
        C_in = n.shape[1]
        s = laplace_weight(N, C_in, C_in)
        N,A,B,V,M,W,S = self.rfft_xd(n,a,b,v,m,w,s)
        eta1 = reshape_params3(eta1, N)
        eta3 = reshape_params3(eta3, N)
        l1 = eta3/eta1

        _S = cconj(S)
        _N = cmul(S,(cmul(_S,A)+B-V)) + l1 * (M+W)

        factor1 = _N / l1
        numerator = cmul(_S, _N)
        denominator = csum(l1 * cmul(_S, S), l1.squeeze(-1)**2)
        factor2 = cmul(S, cdiv(numerator, denominator))
        N = (factor1 - factor2).mean(1)
        return torch.fft.irfft2(torch.complex(N[..., 0],N[..., 1]), dim=(-2,-1)).real
    def rfft(self,x):
        X = torch.fft.rfft2(x, dim=(-2,-1),norm=None)
        X = torch.stack((X.real, X.imag), -1)
        return X.unsqueeze(1)
    def rfft_xd(self,n,a,b,v,m,w,s):
        N = self.rfft(n)
        A = self.rfft(a)
        B = self.rfft(b)
        V = self.rfft(v)
        M = self.rfft(m)
        W = self.rfft(w)
        S = p2o(s, n.shape[-2:])
        return N,A,B,V,M,W,S
class Update_z(nn.Module):
    def __init__(self,
                 in_nc = 3,
                 nc_x= [64, 128, 256],
                 nb= 4):
        super(Update_z, self).__init__()
        self.encode = nn.Sequential(
            B.conv(in_nc+1, nc_x[0], bias=False, mode='C'),
            B.conv(nc_x[0], nc_x[0], bias=False, mode='R'),
            B.conv(nc_x[0], nc_x[0], bias=False, mode='C'),)
        self.m_down1 = B.sequential(
            *[B.ResBlock(nc_x[0], nc_x[0], bias=False, mode='CRC') for _ in range(nb)],
            B.downsample_strideconv(nc_x[0], nc_x[1], bias=False, mode='2'))
        self.m_down2 = B.sequential(
            *[B.ResBlock(nc_x[1], nc_x[1], bias=False, mode='CRC') for _ in range(nb)],
            B.downsample_strideconv(nc_x[1], nc_x[2], bias=False, mode='2'))
        self.m_body = B.sequential(*[
            B.ResBlock(nc_x[-1], nc_x[-1], bias=False, mode='CRC')for _ in range(nb)])
        self.m_up2 = B.sequential(
            B.upsample_convtranspose(nc_x[-1], nc_x[1], bias=False, mode='2'),
            *[B.ResBlock(nc_x[1], nc_x[1], bias=False, mode='CRC') for _ in range(nb)])
        self.m_up1 = B.sequential(
            B.upsample_convtranspose(nc_x[1], nc_x[0], bias=False, mode='2'),
            *[B.ResBlock(nc_x[0], nc_x[0], bias=False, mode='CRC') for _ in range(nb)])

        self.m_tail = B.conv(nc_x[0], in_nc, bias=False, mode='C')
    def forward(self, x,gamma):
        gamma = reshape_params4(gamma, x)
        x0 = x
        x1 = self.encode(torch.cat([x, gamma], dim=1))
        x2 = self.m_down1(x1)
        x3 = self.m_down2(x2)
        x = self.m_body(x3)
        x = self.m_up2(x + x3)
        x = self.m_up1(x + x2)
        x = self.m_tail(x + x1) + x0
        return x
class Generate_H(nn.Module):
    def __init__(self, nc_d = 64, in_nc: int = 1):
        super(Generate_H, self).__init__()
        self.gh1 = NetD(nc_d,in_nc)
        self.gh2 = NetD(nc_d,in_nc)
        self.gh3 = NetD(nc_d,in_nc)
        self.fusion = CoordAtt(in_nc,nc_d)

    def forward(self, x,y,z):
        x1 = self.gh1(x,y)
        x2 = self.gh2(z,y)
        x3 = self.gh3(x,z)
        out = self.fusion(x1, x2, x3)
        return out
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
class CoordAtt(nn.Module):
    def __init__(self, inp, mip, reduction=32):
        super(CoordAtt, self).__init__()
        self.inp = inp
        self.pool_h = nn.AdaptiveAvgPool2d((None, 1))
        self.pool_w = nn.AdaptiveAvgPool2d((1, None))

        self.conv1 = nn.Conv2d(inp*3, mip, kernel_size=1, stride=1, padding=0)
        self.act = h_swish()

        self.conv_h = nn.Conv2d(mip, inp*3, kernel_size=1, stride=1, padding=0)
        self.conv_w = nn.Conv2d(mip, inp*3, kernel_size=1, stride=1, padding=0)
        self.conv_end = nn.Conv2d(inp*3, inp, kernel_size=1, stride=1, padding=0)
        self.self_SA_Enhance = SA_Enhance()

    def forward(self, x1, x2, x3):
        x = torch.cat((x1, x2, x3), dim=1)

        n, c, h, w = x.size()
        x_h = self.pool_h(x)
        x_w = self.pool_w(x).permute(0, 1, 3, 2)

        y = torch.cat([x_h, x_w], dim=2)#torch.Size([10, 3072, 24, 1])
        y = self.conv1(y)
        y = self.act(y)

        x_h, x_w = torch.split(y, [h, w], dim=2)
        x_w = x_w.permute(0, 1, 3, 2)

        a_h = self.conv_h(x_h).sigmoid()
        a_w = self.conv_w(x_w).sigmoid()

        out_ca = x * a_w * a_h
        out_sa = self.self_SA_Enhance(out_ca)
        out = x.mul(out_sa)
        out = self.conv_end(out)
        return out
class Fusion(nn.Module):
    def __init__(self, dim_in,dim_mid):
        super(Fusion, self).__init__()
        self.conv1 = nn.Conv2d(dim_in, dim_mid, kernel_size=1, stride=1, padding="same", groups=1, bias=False)
        self.conv2 = nn.Conv2d(dim_mid, dim_in * 2, kernel_size=1, stride=1, padding="same", groups=1, bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.sigmoid = nn.Sigmoid()
        self.pool = nn.AdaptiveAvgPool2d(1)
    def forward(self,x,y):
        x_pool = self.pool(x)
        y_pool = self.pool(y)
        att = x_pool * y_pool
        att = self.relu(self.conv1(att))
        x_pool,y_pool = self.sigmoid(self.conv2(att)).chunk(2,dim=1)
        return x * x_pool + y * y_pool
class NetD(nn.Module):
    def __init__(self, nc_d = 64, in_nc: int = 1):
        super(NetD, self).__init__()
        self.mlp = nn.Sequential(
            nn.Conv2d(in_nc * 2, nc_d, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_d, nc_d, 3, padding=1))
        self.mlp2 = nn.Sequential(
            nn.Conv2d(nc_d, nc_d, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_d, nc_d, 3, padding=1))
        self.mlp3 = nn.Sequential(
            nn.Conv2d(nc_d, nc_d, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(nc_d, in_nc, 3, padding=1))
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x,lam):
        x = torch.cat([x, lam], dim=1)
        x = self.relu(self.mlp(x))
        x = self.relu(self.mlp2(x))
        x = self.mlp3(x)
        return x
class Update_bv(nn.Module):
    def __init__(self,C_in,nc_x=[64, 128, 256]):
        super(Update_bv, self).__init__()
        kernel = [[-1, -1, -1],
                  [-1, 8, -1],
                  [-1, -1, -1]]
        kernel = torch.FloatTensor(kernel).expand(1, C_in, 3, 3)
        self.weight = nn.Parameter(data=kernel, requires_grad=False)
        self.up_b = NetD(nc_x[0],in_nc=C_in)
    def forward(self,z,a,u,lam):
        lam = reshape_params4(lam, z)
        out_z = F.conv2d(z, self.weight, padding=1)
        out_a = F.conv2d(a, self.weight, padding=1)
        out_c = out_z - out_a + u
        out_c = self.up_b(out_c,lam)
        out_u = u + out_z - out_a - out_c
        return out_c,out_u
class Stage(nn.Module):
    def __init__(self, in_nc=3, nc_x=[64, 128, 256],nb=4):
        super(Stage, self).__init__()
        self.up_z = Update_z(in_nc=in_nc, nc_x=nc_x, nb=nb)
        self.up_m = Update_m()
        self.up_n = Update_n()
        self.bv = Update_bv(C_in = in_nc,nc_x=nc_x)
        self.generate_h = Generate_H(nc_x[0],in_nc=in_nc)

    def forward(self,x,y,z,m,n,b,v,u,w,lambda1=1,lambda2=1,eta1=1,eta2=1,eta3=1,gamma=1):
        H = self.generate_h(x,y,z)
        # print(x.shape,H.shape)
        m = self.up_m(z, u, n, w, H, x, lambda1=lambda1, eta2=eta2, eta3=eta3)#z, u, n, w, H, x, lambda1=1, eta2=1, eta3=1
        n = self.up_n(n,y,b,v,m,w,eta1=eta1,eta3=eta3)#n,a,b,v,m,w,eta1=1,eta3=1
        b,v = self.bv(n,y,v,eta1/lambda2)
        z = self.up_z(m-u, eta2/gamma)
        u = u + z -m
        w = w + m -n
        return z, m, n, b, v, u, w,H

class TLCNet(nn.Module):
    def __init__(self, in_channels=1, out_channels=1, num_of_layers=5,nc_x=[32, 64, 128],nb=2,d_size=3):
        super(TLCNet, self).__init__()
        self.num_of_layers = num_of_layers
        self.head = HeadNet(in_nc=in_channels, nc_x=nc_x, out_nc=out_channels, d_size=d_size)
        self.hypa_list: nn.ModuleList = nn.ModuleList()
        self.update: nn.ModuleList = nn.ModuleList()
        for i in range(num_of_layers):
            self.hypa_list.append(HyPaNet(in_nc=in_channels, out_nc=6))
            self.update.append(Stage(in_nc=in_channels, nc_x=nc_x, nb=nb))
    def forward(self, x,y):
        # c = xy.shape[1]
        # x = xy[:, :1, :, :]
        # y = xy[:, 1:c, :, :]
        h, w = y.size()[-2:]
        paddingBottom = int(ceil(h / 8) * 8 - h)
        paddingRight = int(ceil(w / 8) * 8 - w)
        y = F.pad(y, [0, paddingRight, 0, paddingBottom], mode='circular')
        x = F.pad(x, [0, paddingRight, 0, paddingBottom], mode='circular')

        z,m,n,b,v,u,w = self.head(x,y)
        preds = []
        preds_else = []
        for i in range(self.num_of_layers):
            hypas = self.hypa_list[i](z,y)
            lambda1 = hypas[:, 0:1].unsqueeze(-1)
            lambda2 = hypas[:, 1:2].unsqueeze(-1)
            eta1 = hypas[:, 2:3].unsqueeze(-1)
            eta2 = hypas[:, 3:4].unsqueeze(-1)
            eta3 = hypas[:, 4:5].unsqueeze(-1)
            gamma = hypas[:, 5:6].unsqueeze(-1)
            z, m, n, b, v, u, w, H = self.update[i](x,y,z,m,n,b,v,u,w,lambda1, lambda2, eta1,eta2,eta3, gamma)
            preds.append(z)
            preds_else.append([m,n,b,v,u,w,H])
        return preds,preds_else
def data_process_npy(data_in):
    data_out=data_in.detach().float().cpu().numpy()
    data_out=np.transpose(data_out,(0,2,3,1))
    data_out = data_out.squeeze()
    return data_out
if __name__ == '__main__':
    net = PxAy()
    # print(net)
    x = torch.rand([10, 1, 128, 128])
    y = torch.rand([10, 1, 128, 200])
    xy = torch.rand([10, 2, 128, 128])

    # x = cv2.imread('/mnt/ssd1/XJY/GDSR/NYUV2/test/Depth/001.png',0)
    # y = cv2.imread('/mnt/ssd1/XJY/GDSR/NYUV2/test/RGB_Y/001.png',0)
    # x = torch.from_numpy(x).type(torch.float32)
    # x = x.unsqueeze(0).unsqueeze(0)
    # y = torch.from_numpy(y).type(torch.float32)
    # y = y.unsqueeze(0).unsqueeze(0)
    # xy = torch.cat([x,y], dim=1)
    a_out = net(xy)
    # print(a_out.shape)
    macs, params = get_model_complexity_info(net, (2, 128,128), as_strings=True,print_per_layer_stat=False, verbose=True)
    print('{:<30}  {:<8}'.format('Computational complexity: ', macs))
    print('{:<30}  {:<8}'.format('Number of parameters: ', params))
