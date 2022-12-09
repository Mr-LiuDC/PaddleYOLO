# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from ppdet.core.workspace import register
from ..bbox_utils import bbox_iou

__all__ = ['YOLOv5Loss']


@register
class YOLOv5Loss(nn.Layer):
    __shared__ = ['num_classes']

    def __init__(self,
                 num_classes=80,
                 downsample_ratios=[8, 16, 32],
                 balance=[4.0, 1.0, 0.4],
                 box_weight=0.05,
                 obj_weight=1.0,
                 cls_weght=0.5,
                 bias=0.5,
                 anchor_t=4.0,
                 label_smooth_eps=0.):
        super(YOLOv5Loss, self).__init__()
        self.num_classes = num_classes
        self.balance = balance
        self.na = 3  # not len(anchors)
        self.gr = 1.0

        self.BCEcls = nn.BCEWithLogitsLoss(
            pos_weight=paddle.to_tensor([1.0]), reduction="mean")
        self.BCEobj = nn.BCEWithLogitsLoss(
            pos_weight=paddle.to_tensor([1.0]), reduction="mean")

        self.loss_weights = {
            'box': box_weight,
            'obj': obj_weight,
            'cls': cls_weght,
        }

        eps = label_smooth_eps if label_smooth_eps > 0 else 0.
        self.cls_pos_label = 1.0 - 0.5 * eps
        self.cls_neg_label = 0.5 * eps

        self.downsample_ratios = downsample_ratios
        self.bias = bias  # named 'g' in torch yolov5
        self.off = paddle.to_tensor(
            [
                [0, 0],
                [1, 0],
                [0, 1],
                [-1, 0],
                [0, -1],  # j,k,l,m
            ],
            dtype='float32') * bias
        self.anchor_t = anchor_t

    def build_targets(self, outputs, targets, anchors):
        # targets['gt_class'] [bs, max_gt_nums, 1]
        # targets['gt_bbox'] [bs, max_gt_nums, 4]
        # targets['pad_gt_mask'] [bs, max_gt_nums, 1]
        gt_nums = targets['pad_gt_mask'].sum(1).squeeze(-1)
        nt = int(sum(gt_nums))
        na = anchors.shape[1]  # not len(anchors)
        tcls, tbox, indices, anch = [], [], [], []

        gain = paddle.ones([7])  # normalized to gridspace gain
        ai = paddle.tile(paddle.arange(na).reshape([na, 1]), [1, nt]) * 1.0

        batch_size = outputs[0].shape[0]
        gt_labels = []
        for idx in range(batch_size):
            gt_num = int(gt_nums[idx])
            if gt_num == 0:
                continue
            gt_bbox = targets['gt_bbox'][idx][:gt_num]
            gt_class = targets['gt_class'][idx][:gt_num] * 1.0
            img_idx = paddle.tile(
                paddle.to_tensor([idx]).reshape([-1, 1]), [gt_num, 1]) * 1.0
            gt_labels.append(paddle.concat([img_idx, gt_class, gt_bbox], 1))
        if (len(gt_labels)):
            gt_labels = paddle.concat(gt_labels)
        else:
            gt_labels = paddle.zeros([0, 6])

        targets_labels = paddle.concat(
            [paddle.tile(gt_labels.unsqueeze(0), [na, 1, 1]), ai[:, :, None]],
            2)
        g = self.bias  # 0.5

        for i in range(len(anchors)):
            anchor = anchors[i] / self.downsample_ratios[i]
            gain[2:6] = paddle.to_tensor(
                outputs[i].shape, dtype='float32')[[3, 2, 3, 2]]  # xyxy gain

            # Match targets_labels to
            t = targets_labels * gain
            if nt:
                # Matches
                r = t[:, :, 4:6] / anchor[:, None]
                j = paddle.maximum(r, 1 / r).max(2) < self.anchor_t
                t = t[j]  # filter

                # Offsets
                gxy = t[:, 2:4]  # grid xy
                gxi = gain[[2, 3]] - gxy  # inverse
                j, k = ((gxy % 1 < g) & (gxy > 1)).T
                l, m = ((gxi % 1 < g) & (gxi > 1)).T
                # paddle stack: bool->int32
                # paddle masked_select: int32->bool
                j_mask = paddle.cast(
                    paddle.stack([
                        paddle.ones_like(j) * 1.0, j * 1.0, k * 1.0, l * 1.0,
                        m * 1.0
                    ], 0), 'int32')

                j7_mask = paddle.cast(
                    paddle.tile(j_mask.unsqueeze(-1), [1, 1, 7]), 'bool')
                t = paddle.masked_select(paddle.tile(t, [5, 1, 1]),
                                         j7_mask).reshape([-1, 7])

                j2_mask = paddle.cast(
                    paddle.tile(j_mask.unsqueeze(-1), [1, 1, 2]), 'bool')
                offsets = paddle.masked_select(
                    (paddle.zeros_like(gxy)[None] + self.off[:, None]),
                    j2_mask).reshape([-1, 2])
            else:
                t = targets_labels[0]
                offsets = 0

            # Define
            b, c = t[:, :2].astype('int64').T  # image, class
            gxy = t[:, 2:4]  # grid xy
            gwh = t[:, 4:6]  # grid wh
            gij = (gxy - offsets).astype('int64')
            gi, gj = gij.T  # grid xy indices

            # Append
            a = t[:, 6].astype('int64')  # anchor indices
            gj, gi = gj.clip(0, gain[3] - 1), gi.clip(0, gain[2] - 1)
            indices.append((b, a, gj, gi))
            tbox.append(paddle.concat([gxy - gij, gwh], 1))
            anch.append(anchor[a])
            tcls.append(c)
        return tcls, tbox, indices, anch

    def yolov5_loss(self, pi, t_cls, t_box, t_indices, t_anchor, balance):
        loss = dict()
        b, a, gj, gi = t_indices  # image, anchor, gridy, gridx
        n = b.shape[0]  # number of targets
        tobj = paddle.zeros_like(pi[:, :, :, :, 4])
        loss_box = paddle.to_tensor([0.])
        loss_cls = paddle.to_tensor([0.])
        if n:
            mask = paddle.stack([b, a, gj, gi], 1)
            ps = pi.gather_nd(mask)
            # Regression
            pxy = F.sigmoid(ps[:, :2]) * 2 - 0.5
            pwh = (F.sigmoid(ps[:, 2:4]) * 2)**2 * t_anchor
            pbox = paddle.concat((pxy, pwh), 1)
            iou = bbox_iou(pbox.T, t_box.T, x1y1x2y2=False, ciou=True)
            loss_box = (1.0 - iou).mean()

            # Objectness
            score_iou = paddle.cast(iou.detach().clip(0), tobj.dtype)
            with paddle.no_grad():
                x = paddle.gather_nd(tobj, mask)
                tobj = paddle.scatter_nd_add(
                    tobj, mask, (1.0 - self.gr) + self.gr * score_iou - x)

            # Classification
            t = paddle.full_like(ps[:, 5:], self.cls_neg_label)
            t[range(n), t_cls] = self.cls_pos_label
            loss_cls = self.BCEcls(ps[:, 5:], t)

        obji = self.BCEobj(pi[:, :, :, :, 4], tobj)  # [bs, 3, h, w]

        loss_obj = obji * balance

        loss['loss_box'] = loss_box * self.loss_weights['box']
        loss['loss_obj'] = loss_obj * self.loss_weights['obj']
        loss['loss_cls'] = loss_cls * self.loss_weights['cls']
        return loss

    def forward(self, inputs, targets, anchors):
        yolo_losses = dict()
        tcls, tbox, indices, anch = self.build_targets(inputs, targets, anchors)

        for i, (p_det, balance) in enumerate(zip(inputs, self.balance)):
            t_cls = tcls[i]
            t_box = tbox[i]
            t_anchor = anch[i]
            t_indices = indices[i]

            bs, ch, h, w = p_det.shape
            pi = p_det.reshape((bs, self.na, -1, h, w)).transpose(
                (0, 1, 3, 4, 2))

            yolo_loss = self.yolov5_loss(pi, t_cls, t_box, t_indices, t_anchor,
                                         balance)

            for k, v in yolo_loss.items():
                if k in yolo_losses:
                    yolo_losses[k] += v
                else:
                    yolo_losses[k] = v

        loss = 0
        for k, v in yolo_losses.items():
            loss += v

        batch_size = inputs[0].shape[0]
        num_gpus = targets.get('num_gpus', 8)
        yolo_losses['loss'] = loss * batch_size * num_gpus
        return yolo_losses
