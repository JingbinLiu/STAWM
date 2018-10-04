import os
import torch
import torchvision
from torchvision import transforms
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from memory import Memory

import tb_modules as tm

import torchbearer
from torchbearer import Trial, callbacks

import visualise

MU = torchbearer.state_key('mu')
LOGVAR = torchbearer.state_key('logvar')
STAGES = torchbearer.state_key('stages')


class Block(nn.Module):
    def __init__(self, in_planes, out_planes, stride=1, padding=0):
        super(Block, self).__init__()
        self.conv = nn.Conv2d(in_planes, out_planes, kernel_size=3, padding=padding, stride=stride, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        torch.nn.init.xavier_uniform_(self.conv.weight)

    def forward(self, x):
        out = F.relu(self.bn(self.conv(x)))
        return out


class InverseBlock(nn.Module):
    def __init__(self, in_planes, out_planes, stride=1, last=False, output_padding=0):
        super(InverseBlock, self).__init__()
        self.last = last
        self.conv = nn.ConvTranspose2d(in_planes, out_planes, kernel_size=3, output_padding=output_padding, stride=stride, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        torch.nn.init.xavier_uniform_(self.conv.weight)

    def forward(self, x):
        if not self.last:
            out = F.relu(self.bn(self.conv(x)))
        else:
            out = self.bn(self.conv(x))
        return out


class ContextNet(nn.Module):
    def __init__(self):
        super(ContextNet, self).__init__()
        self.conv1 = Block(3, 64, stride=2)
        self.conv2 = Block(64, 128, stride=2)
        self.conv3 = Block(128, 256, stride=2)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = x.view(x.size(0), -1)
        return x


class GlimpseNet(nn.Module):
    def __init__(self):
        super(GlimpseNet, self).__init__()
        self.conv1 = Block(3, 64)
        self.conv2 = Block(64, 128, stride=2)
        self.conv3 = Block(128, 256, stride=2)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = x.view(x.size(0), -1)
        return x


class GlimpseDecoder(nn.Module):
    def __init__(self, h, w):
        super(GlimpseDecoder, self).__init__()
        self.h = h
        self.w = w

        self.conv1 = InverseBlock(256, 128)
        self.conv2 = InverseBlock(128, 64, stride=2, output_padding=1)
        self.conv3 = InverseBlock(64, 3, last=True, stride=2, output_padding=1)

    def forward(self, x):
        x = x.view(x.size(0), 256, self.h, self.w)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        return x


class CifarDraw(nn.Module):
    def __init__(self, count, memory_size, output_stages=False):
        super(CifarDraw, self).__init__()

        self.output_stages = output_stages

        self.memory = Memory(output_inverse=True, hidden_size=memory_size, memory_size=memory_size, glimpse_size=16, g_down=1024, c_down=2304, context_net=ContextNet(), glimpse_net=GlimpseNet())

        self.count = count
        self.qdown = nn.Linear(2304, memory_size)

        self.decoder = GlimpseDecoder(2, 2)

        self.drop = nn.Dropout(0.3)

        self.qdown = nn.Linear(2304, memory_size)

        self.mu = nn.Linear(memory_size, 16)
        self.var = nn.Linear(memory_size, 16)

        self.sup = nn.Linear(16, 1024)

        if output_stages:
            self.square = visualise.red_square(16, width=1).unsqueeze(0).cuda()

    def sample(self, mu, logvar):
        std = logvar.div(2).exp_()
        eps = std.data.new(std.size()).normal_()
        return mu + std * eps

    def forward(self, x, state=None):
        image = x
        canvas = torch.zeros_like(x.data)

        x, context = self.memory.init(image)

        c_data = context.data
        query = F.relu6(self.qdown(c_data))

        mu = []
        var = []
        stages = []

        for i in range(self.count):
            x, inverse = self.memory.glimpse(x, image)
            out = self.memory(query)
            o_mu = self.mu(out)
            o_var = self.var(out)
            mu.append(o_mu)
            var.append(o_var)
            out = self.sample(o_mu, o_var)
            out = F.relu(self.sup(out))
            out = self.decoder(out)

            inverse = inverse.view(out.size(0), 2, 3)

            grid = F.affine_grid(inverse, canvas.size())
            out = F.grid_sample(out, grid)

            canvas += out

            if self.output_stages:
                square = self.square.clone().repeat(out.size(0), 1, 1, 1)
                square = F.grid_sample(square, grid)

                stage_image = canvas.data.clone().sigmoid()
                stage_image = stage_image + square
                stage_image = stage_image.clamp(0, 1)
                stages.append(stage_image.unsqueeze(1))

        if state is not None:
            state[torchbearer.Y_TRUE] = image
            state[MU] = torch.cat(mu, dim=1)
            state[LOGVAR] = torch.cat(var, dim=1)
            if self.output_stages:
                stages.append(image.clone().unsqueeze(1))
                state[STAGES] = torch.cat(stages, dim=1)

        return canvas.sigmoid()


def draw(count, memory_size, file, device='cuda'):
    transform_test = transforms.Compose([
        transforms.ToTensor()
    ])

    testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    testloader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False, num_workers=10)

    base_dir = os.path.join('cifar_' + str(memory_size), "16")

    model = CifarDraw(count, memory_size, output_stages=True)

    optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=0)

    from datetime import datetime
    current_time = datetime.now().strftime('%b%d_%H-%M-%S')

    from visualise import StagesGrid

    trial = Trial(model, optimizer, nn.MSELoss(reduction='sum'), ['loss'], pass_state=True, callbacks=[
        callbacks.TensorBoardImages(comment=current_time, name='Prediction', write_each_epoch=True,
                                    key=torchbearer.Y_PRED, pad_value=1),
        callbacks.TensorBoardImages(comment=current_time + '_cifar', name='Target', write_each_epoch=False,
                                    key=torchbearer.Y_TRUE, pad_value=1),
        StagesGrid('cifar_stages.png', STAGES, 20)
    ]).load_state_dict(torch.load(os.path.join(base_dir, file)), resume=False).with_generators(train_generator=testloader, val_generator=testloader).for_train_steps(1).for_val_steps(1).to(device)

    trial.run()  # Evaluate doesn't work with tensorboard in torchbearer, seems to have been fixed in most recent version


def run(count, memory_size, iteration, device='cuda'):
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor()
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor()
    ])

    trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=128, shuffle=True, num_workers=10)

    testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    testloader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False, num_workers=10)

    base_dir = os.path.join('cifar_' + str(memory_size), "16")

    model = CifarDraw(count, memory_size)

    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=1e-3)

    from datetime import datetime
    current_time = datetime.now().strftime('%b%d_%H-%M-%S')

    trial = Trial(model, optimizer, nn.MSELoss(reduction='sum'), ['acc', 'loss'], pass_state=True, callbacks=[
        tm.kl_divergence(MU, LOGVAR),
        callbacks.MostRecent(os.path.join(base_dir, 'iter_' + str(iteration) + '.{epoch:02d}.pt')),
        callbacks.GradientClipping(5),
        callbacks.ExponentialLR(0.99),
        callbacks.TensorBoardImages(comment=current_time, name='Prediction', write_each_epoch=True,
                                    key=torchbearer.Y_PRED),
        callbacks.TensorBoardImages(comment=current_time + '_cifar', name='Target', write_each_epoch=False,
                                    key=torchbearer.Y_TRUE),
    ]).with_generators(train_generator=trainloader, val_generator=testloader).to(device)

    trial.run(50)


if __name__ == "__main__":
    run(12, 256, 0)
    draw(12, 256, 'iter_0.49.pt')