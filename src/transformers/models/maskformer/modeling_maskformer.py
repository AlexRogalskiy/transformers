# coding=utf-8
# Copyright 2021 Facebook AI Research The HuggingFace Inc. team. All rights reserved.
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
""" PyTorch MaskFormer model."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from multiprocessing.sharedctypes import Value
from numbers import Number
from pprint import pprint
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, Union

import numpy as np
import torch
import torchvision
from torch import Tensor, nn
from torch.nn import functional as F

from einops import rearrange
from einops.einops import repeat
from timm.models.layers import DropPath, to_2tuple, trunc_normal_

from ...file_utils import (
    ModelOutput,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    is_scipy_available,
    is_timm_available,
    is_vision_available,
    replace_return_docstrings,
    requires_backends,
)
from ...modeling_utils import PreTrainedModel
from ...utils import logging
from ..detr import DetrConfig
from ..detr.modeling_detr import DetrDecoder, DetrDecoderOutput
from .configuration_maskformer import ClassSpec, MaskFormerConfig


logger = logging.get_logger(__name__)
import torch.distributed as dist


_CONFIG_FOR_DOC = "MaskFormerConfig"

PREDICTIONS_MASKS_KEY = "preds_masks"
PREDICTIONS_LOGITS_KEY = "preds_logits"
TARGETS_MASKS_KEY = "pixel"
TARGETS_LABELS_KEY = "classes"

# TODO this has to go away!
from detectron2.utils.comm import get_world_size
from scipy.optimize import linear_sum_assignment


@dataclass
class MaskFormerOutput(ModelOutput):
    preds_logits: torch.FloatTensor
    preds_masks: torch.FloatTensor = None
    loss: Optional[torch.FloatTensor] = None
    loss_dict: Optional[Dict] = None


@dataclass
class MaskFormerForSemanticSegmentationOutput(ModelOutput):
    segmentation: torch.FloatTensor = None
    preds_logits: torch.FloatTensor = None
    preds_masks: torch.FloatTensor = None
    loss: Optional[torch.FloatTensor] = None
    loss_dict: Optional[Dict] = None


@dataclass
class MaskFormerForPanopticSegmentationOutput(ModelOutput):
    segmentation: torch.FloatTensor = None
    preds_logits: torch.FloatTensor = None
    preds_masks: torch.FloatTensor = None
    loss: Optional[torch.FloatTensor] = None
    loss_dict: Optional[Dict] = None
    segments: List[PanopticSegmentationSegment] = None


# copied from original implementation
def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True


# refactored from original implementation
def dice_loss(inputs: Tensor, labels: Tensor, num_masks: float) -> Tensor:
    r"""
    Compute the DICE loss, similar to generalized IOU for masks as follow

    $$
        \mathcal{L}_{\text{dice}(x, y) = 1 - \frac{2 * x \cap y }{x \cup y + 1}}

    $$
    In practice, since `labels` is a binary mask, (only 0s and 1s), dice can be computed as follow

    $$
        \mathcal{L}_{\text{dice}(x, y) = 1 - \frac{2 * x * y }{x + y + 1}}
    $$

    Args:
        inputs (Tensor): A tensor representing a mask
        labels (Tensor): A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs (0 for the negative class and 1 for the positive class).


    Returns:
        Tensor: The computed loss
    """
    probs: Tensor = inputs.sigmoid().flatten(1)
    numerator: Tensor = 2 * (probs * labels).sum(-1)
    denominator: Tensor = probs.sum(-1) + labels.sum(-1)
    loss: Tensor = 1 - (numerator + 1) / (denominator + 1)
    loss = loss.sum() / num_masks
    return loss


# copied from original implementation
def sigmoid_focal_loss(
    inputs: Tensor, labels: Tensor, num_masks: int, alpha: float = 0.25, gamma: float = 2
) -> Tensor:
    r"""
       Focal loss proposed in [Focal Loss for Dense Object Detection](https://arxiv.org/abs/1708.02002) originally used in RetinaNet. The loss is computed as follows

    $$
         \mathcal{L}_{\text{focal loss} = -(1 - p_t)^{\gamma}\log{(p_t)}
    $$

    where $CE(p_t) = -\log{(p_t)}}$, CE is the standard Cross Entropy Loss

    Please refer to equation (1,2,3) of the paper for a better understanding.


    Args:
        inputs (Tensor): A float tensor of arbitrary shape.
                The predictions for each example.
        labels (Tensor,): A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs (0 for the negative class and 1 for the positive class).
        alpha (float, optional): Weighting factor in range (0,1) to balance
                positive vs negative examples. Default = -1 (no weighting).
        gamma (float, optional): Exponent of the modulating factor (1 - p_t) to
                balance easy vs hard examples.
    Returns:
        Tensor: The computed loss
    """
    probs: Tensor = inputs.sigmoid()
    cross_entropy_loss: Tensor = F.binary_cross_entropy_with_logits(inputs, labels, reduction="none")
    p_t: Tensor = probs * labels + (1 - probs) * (1 - labels)
    loss: Tensor = cross_entropy_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t: Tensor = alpha * labels + (1 - alpha) * (1 - labels)
        loss = alpha_t * loss

    loss = loss.mean(1).sum() / num_masks
    return loss


def pair_wise_dice_loss(inputs: Tensor, labels: Tensor) -> Tensor:
    """
    A pair wise version of the dice loss, see `dice_loss` for usage

    Args:
        inputs (Tensor): A tensor representing a mask
        labels (Tensor): A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs (0 for the negative class and 1 for the positive class).


    Returns:
        Tensor: The computed loss between each pairs
    """
    inputs: Tensor = inputs.sigmoid()
    # TODO this .flatten seems to be unecessary because the shape is 2d
    inputs: Tensor = inputs.flatten(1)
    # TODO why 1 is not added to the number to avoid numerator = 0 in edge cases?
    numerator: Tensor = 2 * torch.einsum("nc,mc->nm", inputs, labels)
    # using broadcasting to get a [NUM_QUERIES, NUM_CLASSES] matrix
    denominator: Tensor = inputs.sum(-1)[:, None] + labels.sum(-1)[None, :]
    loss: Tensor = 1 - (numerator + 1) / (denominator + 1)
    return loss


def pair_wise_sigmoid_focal_loss(inputs: Tensor, labels: Tensor, alpha: float = 0.25, gamma: float = 2.0) -> Tensor:
    """
    A pair wise version of the focal loss, see `sigmoid_focal_loss` for usage

    Args:
        inputs (Tensor): A tensor representing a mask
        labels (Tensor): A tensor with the same shape as inputs. Stores the binary classification labels for each element in inputs (0 for the negative class and 1 for the positive class).


    Returns:
        Tensor: The computed loss between each pairs
    """
    if alpha < 0:
        raise ValueError(f"alpha must be positive")

    hw: int = inputs.shape[1]

    prob: Tensor = inputs.sigmoid()
    cross_entropy_loss_pos = F.binary_cross_entropy_with_logits(inputs, torch.ones_like(inputs), reduction="none")
    focal_pos: Tensor = ((1 - prob) ** gamma) * cross_entropy_loss_pos
    focal_pos *= alpha

    cross_entropy_loss_neg = F.binary_cross_entropy_with_logits(inputs, torch.zeros_like(inputs), reduction="none")

    focal_neg: Tensor = (prob ** gamma) * cross_entropy_loss_neg
    focal_neg *= 1 - alpha

    loss: Tensor = torch.einsum("nc,mc->nm", focal_pos, labels) + torch.einsum("nc,mc->nm", focal_neg, (1 - labels))

    return loss / hw


# refactored from original implementation
class MaskFormerHungarianMatcher(nn.Module):
    """This class computes an assignment between the labels and the predictions of the network

    For efficiency reasons, the labels don't include the no_object. Because of this, in general,
    there are more predictions than labels. In this case, we do a 1-to-1 matching of the best predictions,
    while the others are un-matched (and thus treated as non-objects).
    """

    def __init__(self, cost_class: float = 1.0, cost_mask: float = 1.0, cost_dice: float = 1.0):
        """Creates the matcher

        Params:
            cost_class: This is the relative weight of the classification error in the matching cost
            cost_mask: This is the relative weight of the focal loss of the binary mask in the matching cost
            cost_dice: This is the relative weight of the dice loss of the binary mask in the matching cost
        """
        super().__init__()
        if cost_class == 0 and cost_mask == 0 and cost_dice == 0:
            raise ValueError("All costs cant be 0")
        self.cost_class = cost_class
        self.cost_mask = cost_mask
        self.cost_dice = cost_dice

    @torch.no_grad()
    def forward(self, outputs: Dict[str, Tensor], labels: Dict[str, Tensor]) -> List[Tuple[Tensor]]:
        """Performs the matching

        Params:
            outputs: This is a dict that contains at least these entries:
                 "pred_logits": Tensor of dim [batch_size, num_queries, num_classes] with the classification logits
                 "pred_masks": Tensor of dim [batch_size, num_queries, H_pred, W_pred] with the predicted masks

            labels: This is a list of labels (len(labels) = batch_size), where each target is a dict containing:
                 "labels": Tensor of dim [num_target_boxes] (where num_target_boxes is the number of ground-truth
                           objects in the target) containing the class labels
                 "masks": Tensor of dim [num_target_boxes, H_gt, W_gt] containing the target masks

        Returns:
            A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected labels (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_target_boxes)
        """

        indices: List[Tuple[np.array]] = []

        preds_masks: Tensor = outputs[PREDICTIONS_MASKS_KEY]
        labels_masks: Tensor = labels[TARGETS_MASKS_KEY]
        preds_probs: Tensor = outputs[PREDICTIONS_LOGITS_KEY].softmax(dim=-1)
        # downsample all masks in one go -> save memory
        labels_masks: Tensor = F.interpolate(labels_masks, size=preds_masks.shape[-2:], mode="nearest")
        # iterate through batch size
        for pred_probs, pred_mask, target_mask, labels in zip(
            preds_probs, preds_masks, labels_masks, labels[TARGETS_LABELS_KEY]
        ):
            # Compute the classification cost. Contrary to the loss, we don't use the NLL,
            # but approximate it in 1 - proba[target class].
            # The 1 is a constant that doesn't change the matching, it can be ommitted.
            cost_class: Tensor = -pred_probs[:, labels]
            # flatten spatial dimension
            pred_mask_flat: Tensor = rearrange(pred_mask, "q h w -> q (h w)")  # [num_queries, H*W]
            target_mask_flat: Tensor = rearrange(target_mask, "c h w -> c (h w)")  # [num_total_labels, H*W]
            # compute the focal loss between each mask pairs -> shape [NUM_QUERIES, CLASSES]
            cost_mask: Tensor = pair_wise_sigmoid_focal_loss(pred_mask_flat, target_mask_flat)
            # Compute the dice loss betwen each mask pairs -> shape [NUM_QUERIES, CLASSES]
            cost_dice: Tensor = pair_wise_dice_loss(pred_mask_flat, target_mask_flat)
            # final cost matrix
            cost_matrix: Tensor = (
                self.cost_mask * cost_mask + self.cost_class * cost_class + self.cost_dice * cost_dice
            )
            # do the assigmented using the hungarian algorithm in scipy
            assigned_indices: Tuple[np.array] = linear_sum_assignment(cost_matrix.cpu())
            indices.append(assigned_indices)

        # TODO this is a little weird, they can be stacked in one tensor
        matched_indices = [
            (torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices
        ]
        return matched_indices

    def __repr__(self):
        head = "Matcher " + self.__class__.__name__
        body = [
            f"cost_class: {self.cost_class}",
            f"cost_mask: {self.cost_mask}",
            f"cost_dice: {self.cost_dice}",
        ]
        _repr_indent = 4
        lines = [head] + [" " * _repr_indent + line for line in body]
        return "\n".join(lines)


# copied from original implementation
class MaskFormerLoss(nn.Module):
    def __init__(
        self,
        num_classes: int,
        matcher: MaskFormerHungarianMatcher,
        weight_dict: Dict[str, float],
        eos_coef: float,
        losses: List[str],
    ):
        """The MaskFormer Loss. The loss is computed very similar to DETR. The process happens in two steps:
        1) we compute hungarian assignment between ground truth masks and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and mask)

        Args:
            num_classes (int): The number of classes
            matcher (MaskFormerHungarianMatcher): A torch module that computes the assigments between the predictions and labels
            weight_dict (Dict[str, float]): A dictionary of weights to be applied to the different losses
            eos_coef (float): TODO no idea
            losses (List[str]): A list of losses to be used TODO probably remove it
        """

        super().__init__()
        requires_backends(self, ["scipy"])
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.eos_coef = eos_coef
        self.losses = losses
        empty_weight: Tensor = torch.ones(self.num_classes + 1)
        empty_weight[-1] = self.eos_coef
        self.register_buffer("empty_weight", empty_weight)

    def loss_labels(
        self, outputs: Dict[str, Tensor], labels: Dict[str, Tensor], indices: Tuple[np.array], num_masks: float
    ) -> Dict[str, Tensor]:
        """Classification loss (NLL)
        # TODO this doc was copied by the authors
        labels dicts must contain the key "labels" containing a tensor of dim [nb_target_masks]
        """

        pred_logits: Tensor = outputs[PREDICTIONS_LOGITS_KEY]
        b, q, _ = pred_logits.shape

        idx = self._get_src_permutation_idx(indices)
        # shape = [BATCH, N_QUERIES]
        target_classes_o: Tensor = torch.cat(
            [target[j] for target, (_, j) in zip(labels[TARGETS_LABELS_KEY], indices)]
        )
        # shape = [BATCH, N_QUERIES]
        target_classes: Tensor = torch.full(
            (b, q), fill_value=self.num_classes, dtype=torch.int64, device=pred_logits.device
        )
        target_classes[idx] = target_classes_o
        loss_ce: Tensor = F.cross_entropy(rearrange(pred_logits, "b q c -> b c q"), target_classes, self.empty_weight)
        losses: Tensor = {"loss_cross_entropy": loss_ce}
        return losses

    def loss_masks(
        self, outputs: Dict[str, Tensor], labels: Dict[str, Tensor], indices: Tuple[np.array], num_masks: int
    ) -> Dict[str, Tensor]:
        """Compute the losses related to the masks: the focal loss and the dice loss.
        labels dicts must contain the key "masks" containing a tensor of dim [nb_target_masks, h, w]
        """
        src_idx = self._get_src_permutation_idx(indices)
        tgt_idx = self._get_tgt_permutation_idx(indices)
        pred_masks = outputs[PREDICTIONS_MASKS_KEY]  # shape [BATCH, NUM_QUERIES, H, W]
        pred_masks = pred_masks[src_idx]  # shape [BATCH * NUM_QUERIES, H, W]
        target_masks = labels[TARGETS_MASKS_KEY]  # shape [BATCH, NUM_QUERIES, H, W]
        target_masks = target_masks[tgt_idx]  # shape [BATCH * NUM_QUERIES, H, W]
        # upsample predictions to the target size, we have to add one dim to use interpolate
        pred_masks = F.interpolate(
            pred_masks[:, None], size=target_masks.shape[-2:], mode="bilinear", align_corners=False
        )
        pred_masks = pred_masks[:, 0].flatten(1)

        target_masks = target_masks.flatten(1)
        target_masks = target_masks.view(pred_masks.shape)
        losses = {
            "loss_mask": sigmoid_focal_loss(pred_masks, target_masks, num_masks),
            "loss_dice": dice_loss(pred_masks, target_masks, num_masks),
        }
        return losses

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute labels following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, labels, indices, num_masks):
        loss_map = {"labels": self.loss_labels, "masks": self.loss_masks}
        if loss not in loss_map:
            raise KeyError(f"{loss} not in loss_map")
        return loss_map[loss](outputs, labels, indices, num_masks)

    def forward(self, outputs: Dict[str, Tensor], labels: Dict[str, Tensor]) -> Dict[str, Tensor]:
        """This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             labels: list of dicts, such that len(labels) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        # TODO in theory here we can just take the `pred_masks` key
        outputs_without_aux = {
            PREDICTIONS_MASKS_KEY: outputs[PREDICTIONS_MASKS_KEY],
            PREDICTIONS_LOGITS_KEY: outputs[PREDICTIONS_LOGITS_KEY],
        }

        # Retrieve the matching between the outputs of the last layer and the labels
        indices = self.matcher(outputs_without_aux, labels)

        # Compute the average number of target masks accross all nodes, for normalization purposes
        num_masks: Number = self.get_num_masks(labels, device=next(iter(outputs.values())).device)

        # Compute all the requested losses
        losses: Dict[str, Tensor] = {}
        for loss in self.losses:
            losses.update(self.get_loss(loss, outputs, labels, indices, num_masks))

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if "auxilary_predictions" in outputs:
            for i, aux_outputs in enumerate(outputs["auxilary_predictions"]):
                indices = self.matcher(aux_outputs, labels)
                for loss in self.losses:
                    l_dict = self.get_loss(loss, aux_outputs, labels, indices, num_masks)
                    l_dict = {k + f"_{i}": v for k, v in l_dict.items()}
                    losses.update(l_dict)

        return losses

    def get_num_masks(self, labels: Dict[str, Tensor], device: torch.device) -> Number:
        # Compute the average number of target masks accross all nodes, for normalization purposes
        num_masks: int = labels[TARGETS_LABELS_KEY].shape[0]
        num_masks_pt: Tensor = torch.as_tensor([num_masks], dtype=torch.float, device=device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_masks_pt)
        num_masks_clamped: Number = torch.clamp(num_masks_pt / get_world_size(), min=1).item()
        return num_masks_clamped


# TODO we could use our implementation of the swin transformer, that looks very similar to the authors' one,
# with the downside of using more time to converting the weights
# copied from original implementation
class Mlp(nn.Module):
    """Multilayer perceptron."""

    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.0):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


# copied from original implementation
def window_partition(x, window_size):
    """
    Args:
        x: (B, H, W, C)
        window_size (int): window size
    Returns:
        windows: (num_windows*B, window_size, window_size, C)
    """
    B, H, W, C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    return windows


# copied from original implementation
def window_reverse(windows, window_size, H, W):
    """
    Args:
        windows: (num_windows*B, window_size, window_size, C)
        window_size (int): Window size
        H (int): Height of image
        W (int): Width of image
    Returns:
        x: (B, H, W, C)
    """
    B = int(windows.shape[0] / (H * W / window_size / window_size))
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    return x


# copied from original implementation
class WindowAttention(nn.Module):
    """Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.
    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(
        self,
        dim,
        window_size,
        num_heads,
        qkv_bias=True,
        qk_scale=None,
        attn_drop=0.0,
        proj_drop=0.0,
    ):

        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wh, Ww
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        # define a parameter table of relative position bias
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads)
        )  # 2*Wh-1 * 2*Ww-1, nH

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
        self.register_buffer("relative_position_index", relative_position_index)

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        trunc_normal_(self.relative_position_bias_table, std=0.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):
        """Forward function.
        Args:
            x: input features with shape of (num_windows*B, N, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        B_, N, C = x.shape
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        q = q * self.scale
        attn = q @ k.transpose(-2, -1)

        relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1
        )  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


# copied from original implementation
class SwinTransformerBlock(nn.Module):
    """Swin Transformer Block.
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        window_size (int): Window size.
        shift_size (int): Shift size for SW-MSA.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Module, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(
        self,
        dim,
        num_heads,
        window_size=7,
        shift_size=0,
        mlp_ratio=4.0,
        qkv_bias=True,
        qk_scale=None,
        drop=0.0,
        attn_drop=0.0,
        drop_path=0.0,
        act_layer=nn.GELU,
        norm_layer=nn.LayerNorm,
    ):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio
        assert 0 <= self.shift_size < self.window_size, "shift_size must in 0-window_size"

        self.norm1 = norm_layer(dim)
        self.attn = WindowAttention(
            dim,
            window_size=to_2tuple(self.window_size),
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop,
        )

        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.H = None
        self.W = None

    def forward(self, x, mask_matrix):
        """Forward function.
        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
            mask_matrix: Attention mask for cyclic shift.
        """
        B, L, C = x.shape
        H, W = self.H, self.W
        assert L == H * W, "input feature has wrong size"

        shortcut = x
        x = self.norm1(x)
        x = x.view(B, H, W, C)

        # pad feature maps to multiples of window size
        pad_l = pad_t = 0
        pad_r = (self.window_size - W % self.window_size) % self.window_size
        pad_b = (self.window_size - H % self.window_size) % self.window_size
        x = F.pad(x, (0, 0, pad_l, pad_r, pad_t, pad_b))
        _, Hp, Wp, _ = x.shape

        # cyclic shift
        if self.shift_size > 0:
            shifted_x = torch.roll(x, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
            attn_mask = mask_matrix
        else:
            shifted_x = x
            attn_mask = None

        # partition windows
        x_windows = window_partition(shifted_x, self.window_size)  # nW*B, window_size, window_size, C
        x_windows = x_windows.view(-1, self.window_size * self.window_size, C)  # nW*B, window_size*window_size, C

        # W-MSA/SW-MSA
        attn_windows = self.attn(x_windows, mask=attn_mask)  # nW*B, window_size*window_size, C

        # merge windows
        attn_windows = attn_windows.view(-1, self.window_size, self.window_size, C)
        shifted_x = window_reverse(attn_windows, self.window_size, Hp, Wp)  # B H' W' C

        # reverse cyclic shift
        if self.shift_size > 0:
            x = torch.roll(shifted_x, shifts=(self.shift_size, self.shift_size), dims=(1, 2))
        else:
            x = shifted_x

        if pad_r > 0 or pad_b > 0:
            x = x[:, :H, :W, :].contiguous()

        x = x.view(B, H * W, C)

        # FFN
        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.mlp(self.norm2(x)))

        return x


# copied from original implementation
class PatchMerging(nn.Module):
    """Patch Merging Layer
    Args:
        dim (int): Number of input channels.
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.reduction = nn.Linear(4 * dim, 2 * dim, bias=False)
        self.norm = norm_layer(4 * dim)

    def forward(self, x, H, W):
        """Forward function.
        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
        """
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"

        x = x.view(B, H, W, C)

        # padding
        pad_input = (H % 2 == 1) or (W % 2 == 1)
        if pad_input:
            x = F.pad(x, (0, 0, 0, W % 2, 0, H % 2))

        x0 = x[:, 0::2, 0::2, :]  # B H/2 W/2 C
        x1 = x[:, 1::2, 0::2, :]  # B H/2 W/2 C
        x2 = x[:, 0::2, 1::2, :]  # B H/2 W/2 C
        x3 = x[:, 1::2, 1::2, :]  # B H/2 W/2 C
        x = torch.cat([x0, x1, x2, x3], -1)  # B H/2 W/2 4*C
        x = x.view(B, -1, 4 * C)  # B H/2*W/2 4*C

        x = self.norm(x)
        x = self.reduction(x)

        return x


# copied from original implementation
class BasicLayer(nn.Module):
    """A basic Swin Transformer layer for one stage.
    Args:
        dim (int): Number of feature channels
        depth (int): Depths of this stage.
        num_heads (int): Number of attention head.
        window_size (int): Local window size. Default: 7.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        downsample (nn.Module | None, optional): Downsample layer at the end of the layer. Default: None
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False.
    """

    def __init__(
        self,
        dim,
        depth,
        num_heads,
        window_size=7,
        mlp_ratio=4.0,
        qkv_bias=True,
        qk_scale=None,
        drop=0.0,
        attn_drop=0.0,
        drop_path=0.0,
        norm_layer=nn.LayerNorm,
        downsample=None,
        use_checkpoint=False,
    ):
        super().__init__()
        self.window_size = window_size
        self.shift_size = window_size // 2
        self.depth = depth
        self.use_checkpoint = use_checkpoint

        # build blocks
        self.blocks = nn.ModuleList(
            [
                SwinTransformerBlock(
                    dim=dim,
                    num_heads=num_heads,
                    window_size=window_size,
                    shift_size=0 if (i % 2 == 0) else window_size // 2,
                    mlp_ratio=mlp_ratio,
                    qkv_bias=qkv_bias,
                    qk_scale=qk_scale,
                    drop=drop,
                    attn_drop=attn_drop,
                    drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                    norm_layer=norm_layer,
                )
                for i in range(depth)
            ]
        )

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(dim=dim, norm_layer=norm_layer)
        else:
            self.downsample = None

    def forward(self, x, H, W):
        """Forward function.
        Args:
            x: Input feature, tensor size (B, H*W, C).
            H, W: Spatial resolution of the input feature.
        """

        # calculate attention mask for SW-MSA
        Hp = int(np.ceil(H / self.window_size)) * self.window_size
        Wp = int(np.ceil(W / self.window_size)) * self.window_size
        img_mask = torch.zeros((1, Hp, Wp, 1), device=x.device)  # 1 Hp Wp 1
        h_slices = (
            slice(0, -self.window_size),
            slice(-self.window_size, -self.shift_size),
            slice(-self.shift_size, None),
        )
        w_slices = (
            slice(0, -self.window_size),
            slice(-self.window_size, -self.shift_size),
            slice(-self.shift_size, None),
        )
        cnt = 0
        for h in h_slices:
            for w in w_slices:
                img_mask[:, h, w, :] = cnt
                cnt += 1

        mask_windows = window_partition(img_mask, self.window_size)  # nW, window_size, window_size, 1
        mask_windows = mask_windows.view(-1, self.window_size * self.window_size)
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
        attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))

        for blk in self.blocks:
            blk.H, blk.W = H, W
            if self.use_checkpoint:
                x = checkpoint.checkpoint(blk, x, attn_mask)
            else:
                x = blk(x, attn_mask)
        if self.downsample is not None:
            x_down = self.downsample(x, H, W)
            Wh, Ww = (H + 1) // 2, (W + 1) // 2
            return x, H, W, x_down, Wh, Ww
        else:
            return x, H, W, x, H, W


# copied from original implementation
class PatchEmbed(nn.Module):
    """Image to Patch Embedding
    Args:
        patch_size (int): Patch token size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(self, patch_size=4, in_chans=3, embed_dim=96, norm_layer=None):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        self.patch_size = patch_size

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):
        """Forward function."""
        # padding
        _, _, H, W = x.size()
        if W % self.patch_size[1] != 0:
            x = F.pad(x, (0, self.patch_size[1] - W % self.patch_size[1]))
        if H % self.patch_size[0] != 0:
            x = F.pad(x, (0, 0, 0, self.patch_size[0] - H % self.patch_size[0]))

        x = self.proj(x)  # B C Wh Ww
        if self.norm is not None:
            Wh, Ww = x.size(2), x.size(3)
            x = x.flatten(2).transpose(1, 2)
            x = self.norm(x)
            x = x.transpose(1, 2).view(-1, self.embed_dim, Wh, Ww)

        return x


# copied from original implementation
class SwinTransformer(nn.Module):
    """Swin Transformer backbone.
        A PyTorch impl of : `Swin Transformer: Hierarchical Vision Transformer using Shifted Windows`  -
          https://arxiv.org/pdf/2103.14030
    Args:
        pretrain_img_size (int): Input image size for training the pretrained model,
            used in absolute postion embedding. Default 224.
        patch_size (int | tuple(int)): Patch size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        depths (tuple[int]): Depths of each Swin Transformer stage.
        num_heads (tuple[int]): Number of attention head of each stage.
        window_size (int): Window size. Default: 7.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float): Override default qk scale of head_dim ** -0.5 if set.
        drop_rate (float): Dropout rate.
        attn_drop_rate (float): Attention dropout rate. Default: 0.
        drop_path_rate (float): Stochastic depth rate. Default: 0.2.
        norm_layer (nn.Module): Normalization layer. Default: nn.LayerNorm.
        ape (bool): If True, add absolute position embedding to the patch embedding. Default: False.
        patch_norm (bool): If True, add normalization after patch embedding. Default: True.
        out_indices (Sequence[int]): Output from which stages.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters.
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False.
    """

    def __init__(
        self,
        pretrain_img_size=224,
        patch_size=4,
        in_chans=3,
        embed_dim=96,
        depths=[2, 2, 6, 2],
        num_heads=[3, 6, 12, 24],
        window_size=7,
        mlp_ratio=4.0,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=0.0,
        attn_drop_rate=0.0,
        drop_path_rate=0.2,
        norm_layer=nn.LayerNorm,
        ape=False,
        patch_norm=True,
        out_indices=(0, 1, 2, 3),
        frozen_stages=-1,
        use_checkpoint=False,
    ):
        super().__init__()

        self.pretrain_img_size = pretrain_img_size
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.out_indices = out_indices
        self.frozen_stages = frozen_stages

        # split image into non-overlapping patches
        self.patch_embed = PatchEmbed(
            patch_size=patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim,
            norm_layer=norm_layer if self.patch_norm else None,
        )

        # absolute position embedding
        if self.ape:
            pretrain_img_size = to_2tuple(pretrain_img_size)
            patch_size = to_2tuple(patch_size)
            patches_resolution = [
                pretrain_img_size[0] // patch_size[0],
                pretrain_img_size[1] // patch_size[1],
            ]

            self.absolute_pos_embed = nn.Parameter(
                torch.zeros(1, embed_dim, patches_resolution[0], patches_resolution[1])
            )
            trunc_normal_(self.absolute_pos_embed, std=0.02)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(
                dim=int(embed_dim * 2 ** i_layer),
                depth=depths[i_layer],
                num_heads=num_heads[i_layer],
                window_size=window_size,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=dpr[sum(depths[:i_layer]) : sum(depths[: i_layer + 1])],
                norm_layer=norm_layer,
                downsample=PatchMerging if (i_layer < self.num_layers - 1) else None,
                use_checkpoint=use_checkpoint,
            )
            self.layers.append(layer)

        num_features = [int(embed_dim * 2 ** i) for i in range(self.num_layers)]
        self.num_features = num_features

        # add a norm layer for each output
        for i_layer in out_indices:
            layer = norm_layer(num_features[i_layer])
            layer_name = f"norm{i_layer}"
            self.add_module(layer_name, layer)

        self._freeze_stages()

    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            self.patch_embed.eval()
            for param in self.patch_embed.parameters():
                param.requires_grad = False

        if self.frozen_stages >= 1 and self.ape:
            self.absolute_pos_embed.requires_grad = False

        if self.frozen_stages >= 2:
            self.pos_drop.eval()
            for i in range(0, self.frozen_stages - 1):
                m = self.layers[i]
                m.eval()
                for param in m.parameters():
                    param.requires_grad = False

    def init_weights(self, pretrained=None):
        """Initialize the weights in backbone.
        Args:
            pretrained (str, optional): Path to pre-trained weights.
                Defaults to None.
        """

        def _init_weights(m):
            if isinstance(m, nn.Linear):
                trunc_normal_(m.weight, std=0.02)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        """Forward function."""
        x = self.patch_embed(x)

        Wh, Ww = x.size(2), x.size(3)
        if self.ape:
            # interpolate the position embedding to the corresponding size
            absolute_pos_embed = F.interpolate(self.absolute_pos_embed, size=(Wh, Ww), mode="bicubic")
            x = (x + absolute_pos_embed).flatten(2).transpose(1, 2)  # B Wh*Ww C
        else:
            x = x.flatten(2).transpose(1, 2)
        x = self.pos_drop(x)
        # the original implementation returned a dictionary of [str, Tensor]
        # we instead return a list of [Tensor]
        features = []
        for i in range(self.num_layers):
            layer = self.layers[i]
            x_out, H, W, x, Wh, Ww = layer(x, Wh, Ww)

            if i in self.out_indices:
                norm_layer = getattr(self, f"norm{i}")
                x_out = norm_layer(x_out)

                out = x_out.view(-1, H, W, self.num_features[i]).permute(0, 3, 1, 2).contiguous()
                features.append(out)

        return features

    def train(self, mode=True):
        """Convert the model into training mode while keep layers freezed."""
        super(SwinTransformer, self).train(mode)
        self._freeze_stages()


class BackboneMixin(nn.Module):
    """This mixin defines a clear way to acces intermediate representation in the subclassing model.
    A list of representations must be returned in the `forward` method, while their sizes in the `outputs_shape`.
    """

    def forward(self, *args, **kwargs) -> List[Tensor]:
        raise NotImplemented

    def outputs_shape(self) -> List[int]:
        raise NotImplemented


class SwinTransformerBackbone(SwinTransformer, BackboneMixin):
    def get_outputs_shape(self) -> List[int]:
        return self.num_features


class ConvLayer(nn.Sequential):
    def __init__(self, in_features: int, out_features: int, kernel_size: int = 3, padding: int = 1):
        """A basic module that executs conv - norm -  in sequence used in MaskFormer.

        Args:
            in_features (int): The number of input features (channels)
            out_features (int): The number of outputs features (channels)
        """
        super().__init__(
            nn.Conv2d(in_features, out_features, kernel_size=kernel_size, padding=padding, bias=False),
            nn.GroupNorm(32, out_features),
            nn.ReLU(inplace=True),
        )


class FPNLayer(nn.Module):
    def __init__(self, in_features: int, lateral_features: int):
        """A Feature Pyramid Network Layer. It creates a feature map by aggregating features from the previous and backbone layer.
        Due to the spartial mismatch, the tensor coming from the previous layer is upsample.

        Args:
            in_features (int): The number of input features (channels)
            lateral_features (int): The number of lateral features (channels)
        """
        super().__init__()
        self.proj = nn.Sequential(
            nn.Conv2d(lateral_features, in_features, kernel_size=1, padding=0, bias=False),
            nn.GroupNorm(32, in_features),
        )

        self.block = ConvLayer(in_features, in_features)

    def forward(self, down: Tensor, left: Tensor) -> Tensor:
        left = self.proj(left)
        down = F.interpolate(down, size=left.shape[-2:], mode="nearest")
        down += left
        down = self.block(down)
        return down


class FPNModel(nn.Module):
    def __init__(self, in_features: int, lateral_widths: List[int], feature_size: int = 256):
        """Feature Pyramid Network, given an input tensor and a set of features map of different feature/spatial size, it creates a list of features map with different the same feature size.

        Args:
            in_features (int): The number of input features (channels)
            lateral_widths (List[int]): A list with the features (channels) size of each lateral connection
            feature_size (int, optional): The features (channels) of the resulting feature maps. Defaults to 256.
        """
        super().__init__()
        self.stem = ConvLayer(in_features, feature_size)
        self.layers = nn.Sequential(*[FPNLayer(feature_size, lateral_width) for lateral_width in lateral_widths[::-1]])

    def forward(self, features: List[Tensor]) -> List[Tensor]:
        fpn_features: List[Tensor] = []
        last_feature: Tensor = features.pop()
        x: Tensor = self.stem(last_feature)
        for layer, left in zip(self.layers, features[::-1]):
            x = layer(x, left)
            fpn_features.append(x)
        return fpn_features


class MaskFormerPixelDecoder(nn.Module):
    def __init__(self, *args, feature_size: int = 256, mask_feature_size: int = 256, **kwargs):
        """Pixel Decoder Module proposed in [Per-Pixel Classification is Not All You Need for Semantic Segmentation](https://arxiv.org/abs/2107.06278). It first run the backbone's feature into a Feature Pyramid Network creating a list of features map. Then, it projects the last one to the correct `mask_size`

        Args:
            feature_size (int, optional): The features (channels) of FPN feature maps. Defaults to 256.
            mask_feature_size (int, optional): The features (channels) of the target masks size $C_{\epsilon}$ in the paper. Defaults to 256.
        """
        super().__init__()
        self.fpn = FPNModel(*args, feature_size=feature_size, **kwargs)
        self.mask_proj = nn.Conv2d(feature_size, mask_feature_size, kernel_size=3, padding=1)

    def forward(self, features: List[Tensor]) -> Tensor:
        fpn_features: List[Tensor] = self.fpn(features)
        # we use the last feature map
        x = self.mask_proj(fpn_features[-1])
        return x


# copied from original implementation, also practically equal to DetrSinePositionEmbedding
class PositionEmbeddingSine(nn.Module):
    """
    This is a more standard version of the position embedding, very similar to the one
    used by the Attention is all you need paper, generalized to work on images.
    """

    def __init__(
        self, num_pos_feats: int = 64, temperature: int = 10000, normalize: bool = False, scale: Optional[float] = None
    ):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * torch.pi
        self.scale = scale

    def forward(self, x: Tensor, mask: Optional[Tensor] = None) -> Tensor:
        if mask is None:
            mask = torch.zeros((x.size(0), x.size(2), x.size(3)), device=x.device, dtype=torch.bool)
        not_mask = ~mask
        y_embed = not_mask.cumsum(1, dtype=torch.float32)
        x_embed = not_mask.cumsum(2, dtype=torch.float32)
        if self.normalize:
            eps = 1e-6
            y_embed = y_embed / (y_embed[:, -1:, :] + eps) * self.scale
            x_embed = x_embed / (x_embed[:, :, -1:] + eps) * self.scale

        dim_t = torch.arange(self.num_pos_feats, dtype=torch.float32, device=x.device)
        dim_t = self.temperature ** (2 * torch.div(dim_t, 2, rounding_mode="floor") / self.num_pos_feats)

        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack((pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()), dim=4).flatten(3)
        pos_y = torch.stack((pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()), dim=4).flatten(3)
        pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)
        return pos


class MaskformerMLPPredictionHead(nn.Sequential):
    def __init__(self, input_dim: int, hidden_dim: int, output_dim: int, num_layers: int = 3):
        """A classic Multi Layer Perceptron (MLP)

        Args:
            input_dim (int): [description]
            hidden_dim (int): [description]
            output_dim (int): [description]
            num_layers (int, optional): [description]. Defaults to 3.
        """
        in_dims: List[int] = [input_dim] + [hidden_dim] * (num_layers - 1)
        out_dims: List[int] = [hidden_dim] * (num_layers - 1) + [output_dim]

        layers: List[nn.Module] = []
        for i, (in_dim, out_dim) in enumerate(zip(in_dims, out_dims)):
            # TODO should name them, e.g. fc, act ...
            layer: nn.Module = nn.Sequential(
                nn.Linear(in_dim, out_dim), nn.ReLU(inplace=True) if i < num_layers - 1 else nn.Identity()
            )
            layers.append(layer)

        super().__init__(*layers)


class MaskFormerPixelLevelModule(nn.Module):
    def __init__(self, config: MaskFormerConfig):
        """Pixel Level Module proposed in [Per-Pixel Classification is Not All You Need for Semantic Segmentation](https://arxiv.org/abs/2107.06278). It runs the input image trough a backbone and a pixel decoder, generating a image features and pixel embeddings."""
        super().__init__()
        self.backbone = SwinTransformerBackbone(
            pretrain_img_size=config.swin_pretrain_img_size,
            patch_size=config.swin_patch_size,
            in_chans=config.swin_in_channels,
            embed_dim=config.swin_embed_dim,
            depths=config.swin_depths,
            num_heads=config.swin_num_heads,
            window_size=config.swin_window_size,
        )
        self.pixel_decoder = MaskFormerPixelDecoder(
            in_features=self.backbone.get_outputs_shape()[-1],
            feature_size=config.fpn_feature_size,
            mask_feature_size=config.mask_feature_size,
            lateral_widths=self.backbone.get_outputs_shape()[:-1],
        )

    def forward(self, x: Tensor) -> Tuple[Tensor, Tensor]:
        features: List[Tensor] = self.backbone(x)
        # the last feature is actually the output from the last layer
        image_features: Tensor = features[-1]
        pixel_embeddings: Tensor = self.pixel_decoder(features)
        return image_features, pixel_embeddings


class MaskFormerTransformerModule(nn.Module):
    def __init__(self, in_features: int, config: MaskFormerConfig):
        super().__init__()
        self.position_embedder = PositionEmbeddingSine(num_pos_feats=config.hidden_size // 2, normalize=True)
        self.queries_embedder = nn.Embedding(config.num_queries, config.hidden_size)
        should_project = in_features != config.hidden_size
        self.input_projection = (
            nn.Conv2d(in_features, config.hidden_size, kernel_size=1) if should_project else nn.Identity()
        )
        # TODO hugly, ask!
        self.detr_decoder = DetrDecoder(
            DetrConfig(**{k: v for k, v in config.__dict__.items() if k.startswith("detr_")})
        )

    def forward(self, image_features: Tensor) -> Tuple[Tensor]:
        image_features = self.input_projection(image_features)
        position_embeddings: Tensor = self.position_embedder(image_features)
        queries_embeddings: Tensor = repeat(self.queries_embedder.weight, "q c -> b q c", b=image_features.shape[0])
        inputs_embeds = torch.zeros_like(queries_embeddings)
        image_features = rearrange(image_features, "b c h w -> (h w) b c")
        position_embeddings = rearrange(position_embeddings, "b c h w -> (h w) b c")

        detr_output: DetrDecoderOutput = self.detr_decoder(
            inputs_embeds=inputs_embeds,
            attention_mask=None,
            encoder_hidden_states=image_features,
            encoder_attention_mask=None,
            position_embeddings=position_embeddings,
            query_position_embeddings=queries_embeddings,
            output_attentions=None,
            output_hidden_states=True,
            return_dict=None,
        )
        # TODO return a tuple is not a good idea, torch.summary will fail
        return detr_output.hidden_states


class MaskFormerSegmentationModule(nn.Module):
    def __init__(self, config: MaskFormerConfig):
        super().__init__()
        # + 1 because we add the "null" class
        self.mask_classification = config.mask_classification
        self.class_predictor = nn.Linear(config.hidden_size, config.num_labels + 1)
        self.mask_embedder = MaskformerMLPPredictionHead(
            config.hidden_size, config.hidden_size, config.mask_feature_size
        )

    def forward(
        self, decoder_outputs: Tuple[Tensor], pixel_embeddings: Tensor, auxilary_loss: bool = True
    ) -> Dict[str, Tensor]:

        out: Dict[str, Tensor] = {}

        # NOTE this code is a little bit cumbersome, an easy fix is to always return a list of predictions, if we have auxilary loss then we are going to return more than one element in the list
        if auxilary_loss:
            stacked_decoder_outputs: Tensor = torch.stack(decoder_outputs)
            classes: Tensor = self.class_predictor(stacked_decoder_outputs)
            out.update({PREDICTIONS_LOGITS_KEY: classes[-1]})
            # get the masks
            mask_embeddings: Tensor = self.mask_embedder(stacked_decoder_outputs)
            # sum up over the channels for each embedding
            binaries_masks: Tensor = torch.einsum("lbqc,   bchw -> lbqhw", mask_embeddings, pixel_embeddings)
            binary_masks: Tensor = binaries_masks[-1]
            # get the auxilary predictions (one for each decoder's layer)
            auxilary_predictions: List[str, Tensor] = []
            # go til [:-1] because the last one is always used
            for aux_binary_masks, aux_classes in zip(binaries_masks[:-1], classes[:-1]):
                auxilary_predictions.append(
                    {PREDICTIONS_MASKS_KEY: aux_binary_masks, PREDICTIONS_LOGITS_KEY: aux_classes}
                )
            out.update({"auxilary_predictions": auxilary_predictions})

        else:
            last_decoder_output: Tensor = decoder_outputs[-1]
            classes: Tensor = self.class_predictor(last_decoder_output)
            out.update({PREDICTIONS_LOGITS_KEY: classes})
            # get the masks
            mask_embeddings: Tensor = self.mask_embedder(last_decoder_output)
            # sum up over the channels
            binary_masks: Tensor = torch.einsum("bqc,   bchw -> bqhw", mask_embeddings, pixel_embeddings)
        out.update({PREDICTIONS_MASKS_KEY: binary_masks})
        return out


def upsample_like(x: Tensor, like: Tensor, mode: str = "bilinear") -> Tensor:
    """An utility function that upsamples `x` to match the dimension of `like`

    Args:
        x (Tensor): The tensor we wish to upsample
        like (Tensor): The tensor we wish to use as size target
        mode (str, optional): The interpolation mode. Defaults to "bilinear".

    Returns:
        Tensor: The upsampled tensor
    """
    _, _, h, w = like.shape
    upsampled: Tensor = F.interpolate(
        x,
        size=(h, w),
        mode=mode,
        align_corners=False,
    )
    return upsampled


class MaskFormerModel(PreTrainedModel):
    config_class = MaskFormerConfig
    base_model_prefix = "model"
    main_input_name = "pixel_values"

    def __init__(self, config: MaskFormerConfig):
        super().__init__(config)
        self.pixel_level_module = MaskFormerPixelLevelModule(config)
        self.transformer_module = MaskFormerTransformerModule(
            in_features=self.pixel_level_module.backbone.get_outputs_shape()[-1], config=config
        )
        self.segmentation_module = MaskFormerSegmentationModule(config)
        self.matcher = MaskFormerHungarianMatcher(
            cost_class=1.0, cost_dice=config.dice_weight, cost_mask=config.mask_weight
        )

        losses = ["labels", "masks"]

        self.weight_dict: Dict[str, float] = {
            "loss_cross_entropy": config.ce_weight,
            "loss_mask": config.mask_weight,
            "loss_dice": config.dice_weight,
        }

        self.criterion = MaskFormerLoss(
            config.num_labels,
            matcher=self.matcher,
            weight_dict=self.weight_dict,
            eos_coef=config.no_object_weight,
            losses=losses,
        )

    def forward(
        self,
        pixel_values: Tensor,
        pixel_mask: Optional[Tensor] = None,
        labels: Optional[Dict[str, Tensor]] = None,
    ) -> MaskFormerOutput:
        image_features, pixel_embeddings = self.pixel_level_module(pixel_values)
        queries = self.transformer_module(image_features)
        outputs: Dict[str, Tensor] = self.segmentation_module(queries, pixel_embeddings)

        loss_dict: Dict[str, Tensor] = {}
        loss: Tensor = None

        if labels is not None:
            loss_dict.update(self.get_loss_dict(outputs, labels))
            loss = self.get_loss(loss_dict)
        else:
            # upsample the masks to match the inputs' spatial dimension
            outputs[PREDICTIONS_MASKS_KEY] = upsample_like(outputs[PREDICTIONS_MASKS_KEY], pixel_values)

        return MaskFormerOutput(**outputs, loss_dict=loss_dict, loss=loss)

    def get_loss_dict(self, outputs: Dict[str, Tensor], labels: Dict[str, Tensor]) -> Dict[str, Tensor]:
        loss_dict: Dict[str, Tensor] = self.criterion(outputs, labels)
        # weight each loss by `self.weight_dict[<LOSS_NAME>]`
        weighted_loss_dict: Dict[str, Tensor] = {
            k: v * self.weight_dict[k] for k, v in loss_dict.items() if k in self.weight_dict
        }
        return weighted_loss_dict

    def get_loss(self, loss_dict: Dict[str, Tensor]) -> Tensor:
        # probably an awkward way to reduce it
        return torch.tensor(list(loss_dict.values()), dtype=torch.float).sum()


class MaskFormerForSemanticSegmentation(nn.Module):
    def __init__(self, config: MaskFormerConfig):
        super().__init__()
        self.model = MaskFormerModel(config)

    def forward(self, *args, **kwargs):
        outputs: MaskFormerOutput = self.model(*args, **kwargs)
        # mask classes has shape [BATCH, QUERIES, CLASSES + 1]
        # remove the null class `[..., :-1]`
        masks_classes: Tensor = outputs.preds_logits.softmax(dim=-1)[..., :-1]
        # mask probs has shape [BATCH, QUERIES, HEIGHT, WIDTH]
        masks_probs: Tensor = outputs.preds_masks.sigmoid()
        # now we want to sum over the queries,
        # $ out_{c,h,w} =  \sum_q p_{q,c} * m_{q,h,w} $
        # where $ softmax(p) \in R^{q, c} $ is the mask classes
        # and $ sigmoid(m) \in R^{q, h, w}$ is the mask probabilities
        # b(atch)q(uery)c(lasses), b(atch)q(uery)h(eight)w(idth)
        segmentation: Tensor = torch.einsum("bqc, bqhw -> bchw", masks_classes, masks_probs)

        return MaskFormerForSemanticSegmentationOutput(segmentation=segmentation, **outputs)


class PanopticSegmentationSegment(TypedDict):
    id: int
    category_id: int
    is_thing: bool
    label: str


class MaskFormerForPanopticSegmentation(MaskFormerForSemanticSegmentation):
    def __init__(
        self,
        config: MaskFormerConfig,
        object_mask_threshold: Optional[float] = 0.8,
        overlap_mask_area_threshold: Optional[float] = 0.8,
    ):
        super().__init__(config)
        self.object_mask_threshold = object_mask_threshold
        self.overlap_mask_area_threshold = overlap_mask_area_threshold

    def remove_low_and_no_objects(
        self, masks: Tensor, scores: Tensor, labels: Tensor
    ) -> Tuple[Tensor, Tensor, Tensor]:
        if not (masks.shape[0] == scores.shape[0] == labels.shape[0]):
            raise ValueError("mask, scores and labels must have the same shape!")

        to_keep: Tensor = labels.ne(self.model.config.num_labels) & (scores > self.object_mask_threshold)

        return masks[to_keep], scores[to_keep], labels[to_keep]

    def forward(self, *args, **kwargs):
        outputs: MaskFormerOutput = self.model(*args, **kwargs)
        preds_logits: Tensor = outputs.preds_logits
        preds_masks: Tensor = outputs.preds_masks

        _, _, h, w = preds_masks.shape

        # for each query, the best score and its index
        pred_scores, pred_labels = F.softmax(preds_logits, dim=-1).max(-1)  # out = [BATH,NUM_QUERIES]
        mask_probs = preds_masks.sigmoid()

        for (mask_probs, pred_scores, pred_labels) in zip(mask_probs, pred_scores, pred_labels):

            # NOTE we can't do it in a batch-wise fashion
            # since to_keep may have different sizes in each prediction
            mask_probs, pred_scores, pred_labels = self.remove_low_and_no_objects(mask_probs, pred_scores, pred_labels)
            we_detect_something: bool = mask_probs.shape[0] > 0

            segmentation: Tensor = torch.zeros((h, w), dtype=torch.int32, device=mask_probs.device)

            segments: List[PanopticSegmentationSegment] = []

            current_segment_id: int = 0

            if we_detect_something:
                # weight each mask by its score
                mask_probs *= pred_scores.view(-1, 1, 1)
                mask_labels: Tensor = mask_probs.argmax(0)
                # mask_labels is a [H,W] where each pixel has a class label
                # basically for each pixel we find out what is the most likely class to be there
                stuff_memory_list: Dict[str, int] = {}
                # this is a map between stuff and segments id, the used it to keep track of the instances of one class

                for k in range(pred_labels.shape[0]):
                    pred_class: int = pred_labels[k].item()
                    # we are checking if pred_class is in the range of the continuous values allowed
                    class_spec: ClassSpec = self.model.config.dataset_metadata.classes[pred_class]
                    is_stuff = not class_spec.is_thing
                    # get the mask associated with the k query
                    mask_k: Tensor = mask_labels == k
                    # create the area, since bool we just need to sum :)
                    mask_k_area: Tensor = mask_k.sum()
                    # this is the area of all the stuff in query k
                    original_area: Tensor = (mask_probs[k] >= 0.5).sum()
                    # find out how much of the all area mask_k is using
                    masks_do_exist: bool = mask_k_area > 0 and original_area > 0

                    if masks_do_exist:
                        area_ratio: float = mask_k_area / original_area
                        mask_k_is_overlapping_enough: bool = area_ratio.item() > self.overlap_mask_area_threshold

                        if mask_k_is_overlapping_enough:
                            # merge stuff regions
                            if pred_class in stuff_memory_list:
                                current_segment_id = stuff_memory_list[pred_class]
                            else:
                                current_segment_id += 1
                            # then we update out mask with the current segment
                            segmentation[mask_k] = current_segment_id
                            segments.append(
                                {
                                    "id": current_segment_id,
                                    "category_id": pred_class,
                                    "is_thing": not is_stuff,
                                    "label": class_spec.label,
                                }
                            )
                            if is_stuff:
                                stuff_memory_list[pred_class] = current_segment_id

            return MaskFormerForPanopticSegmentationOutput(segmentation=segmentation, segments=segments, **outputs)
