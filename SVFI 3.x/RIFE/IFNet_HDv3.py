import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from RIFE.warplayer import warp

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=True),
        nn.PReLU(out_planes)
    )

def conv_bn(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=False),
        nn.BatchNorm2d(out_planes),
        nn.PReLU(out_planes)
    )

class IFBlock(nn.Module):
    def __init__(self, in_planes, c=64):
        super(IFBlock, self).__init__()
        self.conv0 = nn.Sequential(
            conv(in_planes, c // 2, 3, 2, 1),
            conv(c // 2, c, 3, 2, 1),
        )
        self.convblock0 = nn.Sequential(
            conv(c, c),
            conv(c, c)
        )
        self.convblock1 = nn.Sequential(
            conv(c, c),
            conv(c, c)
        )
        self.convblock2 = nn.Sequential(
            conv(c, c),
            conv(c, c)
        )
        self.convblock3 = nn.Sequential(
            conv(c, c),
            conv(c, c)
        )
        self.conv1 = nn.Sequential(
            nn.ConvTranspose2d(c, c // 2, 4, 2, 1),
            nn.PReLU(c // 2),
            nn.ConvTranspose2d(c // 2, 4, 4, 2, 1),
        )
        self.conv2 = nn.Sequential(
            nn.ConvTranspose2d(c, c // 2, 4, 2, 1),
            nn.PReLU(c // 2),
            nn.ConvTranspose2d(c // 2, 1, 4, 2, 1),
        )

    def forward(self, x, flow, scale=1):
        x = F.interpolate(x, scale_factor=1. / scale, mode="bilinear", align_corners=False,
                          recompute_scale_factor=False)
        flow = F.interpolate(flow, scale_factor=1. / scale, mode="bilinear", align_corners=False,
                             recompute_scale_factor=False) * 1. / scale
        feat = self.conv0(torch.cat((x, flow), 1))
        feat = self.convblock0(feat) + feat
        feat = self.convblock1(feat) + feat
        feat = self.convblock2(feat) + feat
        feat = self.convblock3(feat) + feat
        flow = self.conv1(feat)
        mask = self.conv2(feat)
        flow = F.interpolate(flow, scale_factor=scale, mode="bilinear", align_corners=False,
                             recompute_scale_factor=False) * scale
        mask = F.interpolate(mask, scale_factor=scale, mode="bilinear", align_corners=False,
                             recompute_scale_factor=False)
        return flow, mask
        
class IFNet(nn.Module):
    def __init__(self):
        super(IFNet, self).__init__()
        self.block0 = IFBlock(7 + 4, c=90)
        self.block1 = IFBlock(7 + 4, c=90)
        self.block2 = IFBlock(7 + 4, c=90)
        # self.contextnet = Contextnet()
        # self.unet = Unet()

    def get_auto_scale(self, x):
        channel = x.shape[1] // 2
        img0 = x[:, :channel]
        img1 = x[:, channel:]
        warped_img0 = img0
        warped_img1 = img1
        flow = torch.zeros_like(x[:, :4]).to(device)
        mask = torch.zeros_like(x[:, :1]).to(device)
        block = [self.block0, self.block1, self.block2]
        scale = 1.0
        size = x.shape
        threshold = np.sqrt(size[2] * size[3] / 2088960.) * 64
        f0, m0 = block[0](torch.cat((warped_img0[:, :3], warped_img1[:, :3], mask), 1), flow, scale=4)
        if f0[:, :2].abs().max() > threshold and f0[:, 2:4].abs().max() > threshold:
            scale = 0.5
            f0, m0 = block[0](torch.cat((warped_img0[:, :3], warped_img1[:, :3], mask), 1), flow, scale=8)
            if f0[:, :2].abs().max() > threshold and f0[:, 2:4].abs().max() > threshold:
                scale = 0.25
        return scale

    def forward(self, x, scale_list=[4, 2, 1], training=False, ensemble=True, ada=True):
        channel = x.shape[1] // 2
        img0 = x[:, :channel]
        img1 = x[:, channel:]
        flow_list = []
        merged = []
        mask_list = []
        warped_img0 = img0
        warped_img1 = img1
        flow = torch.zeros_like(x[:, :4]).to(device)
        mask = torch.zeros_like(x[:, :1]).to(device)
        loss_cons = 0
        block = [self.block0, self.block1, self.block2]

        for i in range(3):
            f0, m0 = block[i](torch.cat((warped_img0[:, :3], warped_img1[:, :3], mask), 1), flow, scale=scale_list[i])
            if ensemble:
                f1, m1 = block[i](torch.cat((warped_img1[:, :3], warped_img0[:, :3], -mask), 1),
                                  torch.cat((flow[:, 2:4], flow[:, :2]), 1), scale=scale_list[i])
                f0 = (f0 + torch.cat((f1[:, 2:4], f1[:, :2]), 1)) / 2
                m0 = (m0 + (-m1)) / 2
            flow = flow + f0  # TODO all dark frame output in 3.8 model
            mask = mask + m0
            mask_list.append(mask)
            flow_list.append(flow)
            warped_img0 = warp(img0, flow[:, :2])
            warped_img1 = warp(img1, flow[:, 2:4])
            merged.append((warped_img0, warped_img1))
        '''
        c0 = self.contextnet(img0, flow[:, :2])
        c1 = self.contextnet(img1, flow[:, 2:4])
        tmp = self.unet(img0, img1, warped_img0, warped_img1, mask, flow, c0, c1)
        res = tmp[:, 1:4] * 2 - 1
        '''
        mask_list[2] = torch.sigmoid(mask_list[2])
        merged[2] = merged[2][0] * mask_list[2] + merged[2][1] * (1 - mask_list[2])
        return merged, flow
