# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Different model implementation plus a general port for all the models."""
from flax import nn
from jax import random
import jax.numpy as jnp

from jaxnerf.nerf import model_utils


def get_model(key, args):
  return model_dict[args.model](key, args)


class NerfModel(nn.Module):
  """Nerf NN Model with both coarse and fine MLPs."""

  def apply(self, rng_0, rng_1, rays, num_coarse_samples, num_fine_samples,
            use_viewdirs, near, far, noise_std, net_depth, net_width,
            net_depth_condition, net_width_condition, net_activation,
            skip_layer, num_rgb_channels, num_sigma_channels, randomized,
            white_bkgd, deg_point, deg_view, lindisp, rgb_activation,
            sigma_activation):
    """Nerf Model.

    Args:
      rng_0: jnp.ndarray, random number generator for coarse model sampling.
      rng_1: jnp.ndarray, random number generator for fine model sampling.
      rays: jnp.ndarray(float32), [batch_size, 6/9], each ray is a 6-d vector
        where the first 3 dimensions represent the ray origin and the last 3
        dimensions represent the unormalized ray direction. Note that if ndc
        rays are used, rays are 9-d where the extra 3-dimensional vector is the
        view direction before transformed to ndc rays.
      num_coarse_samples: int, the number of samples for coarse nerf.
      num_fine_samples: int, the number of samples for fine nerf.
      use_viewdirs: bool, use viewdirs as a condition.
      near: float, near clip.
      far: float, far clip.
      noise_std: float, std dev of noise added to regularize sigma output.
      net_depth: int, the depth of the first part of MLP.
      net_width: int, the width of the first part of MLP.
      net_depth_condition: int, the depth of the second part of MLP.
      net_width_condition: int, the width of the second part of MLP.
      net_activation: function, the activation function used within the MLP.
      skip_layer: int, add a skip connection to the output vector of every
        skip_layer layers.
      num_rgb_channels: int, the number of RGB channels.
      num_sigma_channels: int, the number of density channels.
      randomized: bool, use randomized stratified sampling.
      white_bkgd: bool, use white background.
      deg_point: degree of positional encoding for positions.
      deg_view: degree of positional encoding for viewdirs.
      lindisp: bool, sampling linearly in disparity rather than depth if true.
      rgb_activation: function, the activation used to generate RGB.
      sigma_activation: function, the activation used to generate density.

    Returns:
      ret: list, [(rgb_coarse, disp_coarse, acc_coarse), (rgb, disp, acc)]
    """
    # Extract viewdirs from the ray array
    if rays.shape[-1] > 6:  # viewdirs different from rays_d
      viewdirs = rays[Ellipsis, -3:]
      rays = rays[Ellipsis, :-3]
    else:  # viewdirs are normalized rays_d
      viewdirs = rays[Ellipsis, 3:6]
    # Stratified sampling along rays
    key, rng_0 = random.split(rng_0)
    z_vals, samples = model_utils.sample_along_rays(key, rays,
                                                    num_coarse_samples, near,
                                                    far, randomized, lindisp)
    samples = model_utils.posenc(samples, deg_point)
    # Point attribute predictions
    if use_viewdirs:
      norms = jnp.linalg.norm(viewdirs, axis=-1, keepdims=True)
      viewdirs = viewdirs / norms
      viewdirs = model_utils.posenc(viewdirs, deg_view)
      raw_rgb, raw_sigma = model_utils.MLP(
          samples,
          viewdirs,
          net_depth=net_depth,
          net_width=net_width,
          net_depth_condition=net_depth_condition,
          net_width_condition=net_width_condition,
          net_activation=net_activation,
          skip_layer=skip_layer,
          num_rgb_channels=num_rgb_channels,
          num_sigma_channels=num_sigma_channels,
      )
    else:
      raw_rgb, raw_sigma = model_utils.MLP(
          samples,
          net_depth=net_depth,
          net_width=net_width,
          net_depth_condition=net_depth_condition,
          net_width_condition=net_width_condition,
          net_activation=net_activation,
          skip_layer=skip_layer,
          num_rgb_channels=num_rgb_channels,
          num_sigma_channels=num_sigma_channels,
      )
    # Add noises to regularize the density predictions if needed
    key, rng_0 = random.split(rng_0)
    raw_sigma = model_utils.add_gaussian_noise(key, raw_sigma, noise_std,
                                               randomized)
    rgb = rgb_activation(raw_rgb)
    sigma = sigma_activation(raw_sigma)
    # Volumetric rendering.
    comp_rgb, disp, acc, weights = model_utils.volumetric_rendering(
        rgb,
        sigma,
        z_vals,
        rays[Ellipsis, 3:6],
        white_bkgd=white_bkgd,
    )
    ret = [
        (comp_rgb, disp, acc),
    ]
    # Hierarchical sampling based on coarse predictions
    if num_fine_samples > 0:
      z_vals_mid = .5 * (z_vals[Ellipsis, 1:] + z_vals[Ellipsis, :-1])
      key, rng_1 = random.split(rng_1)
      z_vals, samples = model_utils.sample_pdf(
          key,
          z_vals_mid,
          weights[Ellipsis, 1:-1],
          rays,
          z_vals,
          num_fine_samples,
          randomized,
      )
      samples = model_utils.posenc(samples, deg_point)
      if use_viewdirs:
        raw_rgb, raw_sigma = model_utils.MLP(samples, viewdirs)
      else:
        raw_rgb, raw_sigma = model_utils.MLP(samples)
      key, rng_1 = random.split(rng_1)
      raw_sigma = model_utils.add_gaussian_noise(key, raw_sigma, noise_std,
                                                 randomized)
      rgb = rgb_activation(raw_rgb)
      sigma = sigma_activation(raw_sigma)
      comp_rgb, disp, acc, unused_weights = model_utils.volumetric_rendering(
          rgb,
          sigma,
          z_vals,
          rays[Ellipsis, 3:6],
          white_bkgd=white_bkgd,
      )
      ret.append((comp_rgb, disp, acc))
    return ret


def nerf(key, args):
  """Neural Randiance Field.

  Args:
    key: jnp.ndarray. Random number generator.
    args: FLAGS class. Hyperparameters of nerf.

  Returns:
    model: nn.Model. Nerf model with parameters.
    state: flax.Module.state. Nerf model state for stateful parameters.
  """
  deg_point = args.deg_point
  deg_view = args.deg_view
  num_coarse_samples = args.num_coarse_samples
  num_fine_samples = args.num_fine_samples
  use_viewdirs = args.use_viewdirs
  near = args.near
  far = args.far
  noise_std = args.noise_std
  randomized = args.randomized
  white_bkgd = args.white_bkgd
  net_depth = args.net_depth
  net_width = args.net_width
  net_depth_condition = args.net_depth_condition
  net_width_condition = args.net_width_condition
  skip_layer = args.skip_layer
  num_rgb_channels = args.num_rgb_channels
  num_sigma_channels = args.num_sigma_channels
  lindisp = args.lindisp

  net_activation = getattr(nn, str(args.net_activation))
  rgb_activation = getattr(nn, str(args.rgb_activation))
  sigma_activation = getattr(nn, str(args.sigma_activation))

  # Assert that rgb_activation always produces outputs in [0, 1], and
  # sigma_activation always produce non-negative outputs.
  x = jnp.exp(jnp.linspace(-90, 90, 1024))
  x = jnp.concatenate([-x[::-1], x], 0)

  rgb = rgb_activation(x)
  if jnp.any(rgb < 0) or jnp.any(rgb > 1):
    raise NotImplementedError(
        "Choice of rgb_activation `{}` produces colors outside of [0, 1]"
        .format(args.rgb_activation))

  sigma = sigma_activation(x)
  if jnp.any(sigma < 0):
    raise NotImplementedError(
        "Choice of sigma_activation `{}` produces negative densities".format(
            args.sigma_activation))

  ray_shape = (args.batch_size, 6 if args.dataset != "llff" else 9)
  model_fn = NerfModel.partial(
      num_coarse_samples=num_coarse_samples,
      num_fine_samples=num_fine_samples,
      use_viewdirs=use_viewdirs,
      near=near,
      far=far,
      noise_std=noise_std,
      net_depth=net_depth,
      net_width=net_width,
      net_depth_condition=net_depth_condition,
      net_width_condition=net_width_condition,
      net_activation=net_activation,
      skip_layer=skip_layer,
      num_rgb_channels=num_rgb_channels,
      num_sigma_channels=num_sigma_channels,
      randomized=randomized,
      white_bkgd=white_bkgd,
      deg_point=deg_point,
      deg_view=deg_view,
      lindisp=lindisp,
      rgb_activation=rgb_activation,
      sigma_activation=sigma_activation)
  with nn.stateful() as init_state:
    unused_outspec, init_params = model_fn.init_by_shape(
        key,
        [
            (key.shape, key.dtype),
            (key.shape, key.dtype),
            (ray_shape, jnp.float32),
        ],
    )
    model = nn.Model(model_fn, init_params)
  return model, init_state


model_dict = {
    "nerf": nerf,
}
