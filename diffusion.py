import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_scatter import scatter

from tqdm import tqdm

from data_utils import lattice_params_to_matrix_torch
from utils import d_log_p_wrapped_normal, BetaScheduler, SigmaScheduler
from cspnet import CSPNet


MAX_ATOMIC_NUM = 100


class SinusoidalTimeEmbeddings(nn.Module):
    """ Attention is all you need. """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, time):
        device = time.device
        half_dim = self.dim // 2
        embeddings = np.log(10000) / (half_dim - 1)
        embeddings = torch.exp(torch.arange(half_dim) * -embeddings).to(device)
        embeddings = time[:, None] * embeddings[None, :]
        embeddings = torch.cat((embeddings.sin(), embeddings.cos()), dim=-1)
        return embeddings


class CSPDiffusion(nn.Module):
    def __init__(self, device) -> None:
        super().__init__()

        self.decoder = CSPNet()
        self.beta_scheduler = BetaScheduler(1000, 'cosine')
        self.sigma_scheduler = SigmaScheduler(1000, 0.005, 0.5)
        self.time_dim = 256
        self.time_embedding = SinusoidalTimeEmbeddings(self.time_dim)
        self.keep_lattice = 1 < 1e-5
        self.keep_coords = 1 < 1e-5
        self.device = device

    def forward(self, batch):
        times = self.beta_scheduler.uniform_sample_t(batch.batch_size, self.device)
        time_emb = self.time_embedding(times)

        alphas_cumprod = self.beta_scheduler.alphas_cumprod[times]
        beta = self.beta_scheduler.betas[times]

        c0 = torch.sqrt(alphas_cumprod)
        c1 = torch.sqrt(1. - alphas_cumprod)

        sigmas = self.sigma_scheduler.sigmas[times]
        sigmas_norm = self.sigma_scheduler.sigmas_norm[times]

        lattices = lattice_params_to_matrix_torch(batch.lengths, batch.angles)
        frac_coords = batch.frac_coords

        anchor_idx = torch.zeros_like(batch.wp_len)
        anchor_idx[1:] = torch.cumsum(batch.wp_len, 0)[:-1]
        rand_l, rand_x = torch.randn_like(lattices), torch.randn([len(batch.wp_len), 3]).to(self.device)
        rand_x = torch.bmm(batch.inv_rotation[anchor_idx], rand_x[..., None]).squeeze()
        rand_x = rand_x.repeat_interleave(batch.wp_len, dim=0)
        rand_x = torch.bmm(batch.rotation, rand_x[..., None]).squeeze()

        input_lattice = c0[:, None, None] * lattices + c1[:, None, None] * rand_l
        sigmas_per_atom = sigmas.repeat_interleave(batch.num_atoms)[:, None]
        sigmas_norm_per_atom = sigmas_norm.repeat_interleave(batch.num_atoms)[:, None]
        input_frac_coords = (frac_coords + sigmas_per_atom * rand_x) % 1.

        if self.keep_coords:
            input_frac_coords = frac_coords

        if self.keep_lattice:
            input_lattice = lattices

        pred_l, pred_x = self.decoder(time_emb, batch.atom_types, input_frac_coords, input_lattice, batch.num_atoms,
                                      batch.batch)

        tar_x = d_log_p_wrapped_normal(sigmas_per_atom * rand_x, sigmas_per_atom) / torch.sqrt(sigmas_norm_per_atom)

        loss_lattice = F.mse_loss(pred_l, rand_l)
        loss_coord = F.mse_loss(pred_x, tar_x)

        loss = (
                1 * loss_lattice +
                1 * loss_coord)

        return {
            'loss': loss,
            'loss_lattice': loss_lattice,
            'loss_coord': loss_coord
        }

    @torch.no_grad()
    def sample(self, batch, step_lr=1e-5):
        batch_size = batch.batch_size

        l_T, x_T = torch.randn([batch_size, 3, 3]).to(self.device), torch.rand([len(batch.wp_len), 3]).to(self.device)
        x_T = torch.bmm(batch.rotation, torch.repeat_interleave(x_T, batch.wp_len, dim=0)[..., None]).squeeze()
        x_T = x_T + batch.translation

        if self.keep_coords:
            x_T = batch.frac_coords

        if self.keep_lattice:
            l_T = lattice_params_to_matrix_torch(batch.lengths, batch.angles)

        time_start = self.beta_scheduler.timesteps

        traj = {time_start: {
            'num_atoms': batch.num_atoms,
            'atom_types': batch.atom_types,
            'frac_coords': x_T % 1.,
            'lattices': l_T
        }}

        for t in tqdm(range(time_start, 0, -1)):

            times = torch.full((batch_size,), t, device=self.device)

            time_emb = self.time_embedding(times)

            alphas = self.beta_scheduler.alphas[t]
            alphas_cumprod = self.beta_scheduler.alphas_cumprod[t]

            sigmas = self.beta_scheduler.sigmas[t]
            sigma_x = self.sigma_scheduler.sigmas[t]
            sigma_norm = self.sigma_scheduler.sigmas_norm[t]

            c0 = 1.0 / torch.sqrt(alphas)
            c1 = (1 - alphas) / torch.sqrt(1 - alphas_cumprod)

            x_t = traj[t]['frac_coords']
            l_t = traj[t]['lattices']

            if self.keep_coords:
                x_t = x_T

            if self.keep_lattice:
                l_t = l_T

            # PC-sampling refers to "Score-Based Generative Modeling through Stochastic Differential Equations"
            # Origin code : https://github.com/yang-song/score_sde/blob/main/sampling.py

            # Corrector

            rand_l = torch.randn_like(l_T) if t > 1 else torch.zeros_like(l_T)
            anchor_idx = torch.zeros_like(batch.wp_len)
            anchor_idx[1:] = torch.cumsum(batch.wp_len, 0)[:-1]
            rand_x = torch.randn([len(batch.wp_len), 3]).to(self.device)
            rand_x = torch.bmm(batch.inv_rotation[anchor_idx], rand_x[..., None]).squeeze()
            rand_x = rand_x.repeat_interleave(batch.wp_len, dim=0)
            rand_x = torch.bmm(batch.rotation, rand_x[..., None]).squeeze()

            step_size = step_lr * (sigma_x / self.sigma_scheduler.sigma_begin) ** 2
            # step_size = step_lr / (sigma_norm * (self.sigma_scheduler.sigma_begin) ** 2)
            std_x = torch.sqrt(2 * step_size)

            pred_l, pred_x = self.decoder(time_emb, batch.atom_types, x_t, l_t, batch.num_atoms, batch.batch)
            scatter_idx = torch.arange(0, len(batch.wp_len), device=self.device).repeat_interleave(batch.wp_len, dim=0)
            pred_x = scatter(pred_x, scatter_idx, dim=0, reduce='mean').repeat_interleave(batch.wp_len, dim=0)

            pred_x = pred_x * torch.sqrt(sigma_norm)

            x_t_minus_05 = x_t - step_size * pred_x + std_x * rand_x if not self.keep_coords else x_t

            l_t_minus_05 = l_t if not self.keep_lattice else l_t

            # Predictor

            rand_l = torch.randn_like(l_T) if t > 1 else torch.zeros_like(l_T)
            anchor_idx = torch.zeros_like(batch.wp_len)
            anchor_idx[1:] = torch.cumsum(batch.wp_len, 0)[:-1]
            rand_x = torch.randn([len(batch.wp_len), 3]).to(self.device)
            rand_x = torch.bmm(batch.inv_rotation[anchor_idx], rand_x[..., None]).squeeze()
            rand_x = rand_x.repeat_interleave(batch.wp_len, dim=0)
            rand_x = torch.bmm(batch.rotation, rand_x[..., None]).squeeze()

            adjacent_sigma_x = self.sigma_scheduler.sigmas[t - 1]
            step_size = (sigma_x ** 2 - adjacent_sigma_x ** 2)
            std_x = torch.sqrt((adjacent_sigma_x ** 2 * (sigma_x ** 2 - adjacent_sigma_x ** 2)) / (sigma_x ** 2))

            pred_l, pred_x = self.decoder(time_emb, batch.atom_types, x_t_minus_05, l_t_minus_05, batch.num_atoms,
                                          batch.batch)
            scatter_idx = torch.arange(0, len(batch.wp_len), device=self.device).repeat_interleave(batch.wp_len, dim=0)
            pred_x = scatter(pred_x, scatter_idx, dim=0, reduce='mean').repeat_interleave(batch.wp_len, dim=0)

            pred_x = pred_x * torch.sqrt(sigma_norm)

            x_t_minus_1 = x_t_minus_05 - step_size * pred_x + std_x * rand_x if not self.keep_coords else x_t

            l_t_minus_1 = c0 * (l_t_minus_05 - c1 * pred_l) + sigmas * rand_l if not self.keep_lattice else l_t

            traj[t - 1] = {
                'num_atoms': batch.num_atoms,
                'atom_types': batch.atom_types,
                'frac_coords': x_t_minus_1 % 1.,
                'lattices': l_t_minus_1
            }

        traj_stack = {
            'num_atoms': batch.num_atoms,
            'atom_types': batch.atom_types,
            'all_frac_coords': torch.stack([traj[i]['frac_coords'] for i in range(time_start, -1, -1)]),
            'all_lattices': torch.stack([traj[i]['lattices'] for i in range(time_start, -1, -1)])
        }

        return traj[0], traj_stack

    def training_step(self, batch, batch_idx: int) -> torch.Tensor:
        output_dict = self(batch)

        loss_lattice = output_dict['loss_lattice']
        loss_coord = output_dict['loss_coord']
        loss = output_dict['loss']

        # self.log_dict(
        #     {'train_loss': loss,
        #      'lattice_loss': loss_lattice,
        #      'coord_loss': loss_coord},
        #     on_step=True,
        #     on_epoch=True,
        #     prog_bar=True,
        # )

        if loss.isnan():
            return None

        return loss

    def test_step(self, batch, batch_idx: int) -> torch.Tensor:
        output_dict = self(batch)

        log_dict, loss = self.compute_stats(output_dict, prefix='test')

        self.log_dict(
            log_dict,
        )
        return loss

    def compute_stats(self, output_dict, prefix):
        loss_lattice = output_dict['loss_lattice']
        loss_coord = output_dict['loss_coord']
        loss = output_dict['loss']

        log_dict = {
            f'{prefix}_loss': loss,
            f'{prefix}_lattice_loss': loss_lattice,
            f'{prefix}_coord_loss': loss_coord
        }

        return log_dict, loss
