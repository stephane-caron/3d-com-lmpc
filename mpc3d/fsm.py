#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Stephane Caron <stephane.caron@normalesup.org>
#
# This file is part of 3d-mpc <https://github.com/stephane-caron/3d-mpc>.
#
# 3d-mpc is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# 3d-mpc is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# 3d-mpc. If not, see <http://www.gnu.org/licenses/>.

import pymanoid

from cwc import compute_cwc_pyparma
from numpy import arange, dot, hstack
from pymanoid import draw_line
from pymanoid.rotations import quat_slerp
from pymanoid.rotations import rotation_matrix_from_quat
from threading import Lock, Thread
from time import sleep as real_sleep

try:
    from hrp4_pymanoid import HRP4 as RobotModel
except ImportError:
    from pymanoid.robots import JVRC1 as RobotModel


def pose_interp(pose0, pose1, t):
    """Linear pose interpolation."""
    pos = pose0[4:] + t * (pose1[4:] - pose0[4:])
    quat = quat_slerp(pose0[:4], pose1[:4], t)
    return hstack([quat, pos])


class FreeLimb(pymanoid.Contact):

    def __init__(self, **kwargs):
        super(FreeLimb, self).__init__(X=0.2, Y=0.1, **kwargs)
        self.end_pose = None
        self.mid_pose = None
        self.start_pose = None

    def reset(self, start_pose, end_pose):
        mid_pose = pose_interp(start_pose, end_pose, .5)
        mid_n = rotation_matrix_from_quat(mid_pose[:4])[0:3, 2]
        mid_pose[4:] += 0.2 * mid_n
        self.set_pose(start_pose)
        self.start_pose = start_pose
        self.mid_pose = mid_pose
        self.end_pose = end_pose

    def update_pose(self, x):
        """Update pose for x \in [0, 1]."""
        if x >= 1.:
            return
        elif x <= .5:
            pose0 = self.start_pose
            pose1 = self.mid_pose
            y = 2. * x
        else:  # .5 < x < 1
            pose0 = self.mid_pose
            pose1 = self.end_pose
            y = 2. * x - 1.
        pos = (1. - y) * pose0[4:] + y * pose1[4:]
        quat = quat_slerp(pose0[:4], pose1[:4], y)
        self.set_pose(hstack([quat, pos]))


class Stance(pymanoid.ContactSet):

    def __init__(self, state, left_foot=None, right_foot=None):
        contacts = {}
        if left_foot:
            contacts['left_foot'] = left_foot
        if right_foot:
            contacts['right_foot'] = right_foot
        foot = left_foot if state[-1] == 'L' else right_foot
        self.com = foot.p + [0., 0., RobotModel.leg_length]
        self.cwc_pyparma = None
        self.state = state
        self.left_foot = left_foot
        self.right_foot = right_foot
        super(Stance, self).__init__(contacts)

    @property
    def is_double_support(self):
        return self.state.startswith('DS')

    @property
    def is_single_support(self):
        return self.state.startswith('SS')

    def get_cwc_pyparma(self, p):
        assert dot(p, p) < 1e-6  # for now, compute all at worl origin
        if self.cwc_pyparma is None:
            self.cwc_pyparma = compute_cwc_pyparma(self, p)
        return self.cwc_pyparma


class StanceFSM(object):

    transitions = {
        'DS-L': 'SS-L',
        'SS-L': 'DS-R',
        'DS-R': 'SS-R',
        'SS-R': 'DS-L'
    }

    def __init__(self, contacts, com, init_state, ss_duration, ds_duration,
                 init_com_offset=None, cyclic=False):
        """
        Create a new finite state machine.

        INPUT:

        - ``contacts`` -- sequence of contacts
        - ``com`` -- PointMass object giving the current position of the COM
        - ``init_state`` -- string giving the initial FSM state
        - ``ss_duration`` -- duration of single-support phases
        - ``ds_duration`` -- duration of double-support phases

        .. NOTE::

            This function updates the position of ``com`` as a side effect.
        """
        assert init_state in ['DS-L', 'DS-R']  # kron
        first_stance = Stance(init_state, contacts[0], contacts[1])
        if init_com_offset is not None:
            first_stance.com += init_com_offset
        com.set_pos(first_stance.com)
        self.com = com
        self.contacts = contacts
        self.cur_stance = first_stance
        self.cyclic = cyclic
        self.ds_duration = ds_duration
        self.free_foot = FreeLimb(visible=False, color='c')
        self.left_foot_traj_handles = []
        self.nb_contacts = len(contacts)
        self.next_contact_id = 2 if init_state == 'DS-R' else 3  # kroooon
        self.rem_time = 0.
        self.right_foot_traj_handles = []
        self.ss_duration = ss_duration
        self.state = init_state
        self.state_time = 0.
        self.thread = None
        self.thread_lock = None

    def start_thread(self, dt, post_step_callback, sleep_fun=None):
        if sleep_fun is None:
            sleep_fun = real_sleep
        self.thread_lock = Lock()
        self.thread = Thread(
            target=self.run_thread, args=(dt, post_step_callback, sleep_fun))
        self.thread.daemon = True
        self.thread.start()

    def pause_thread(self):
        self.thread_lock.acquire()

    def resume_thread(self):
        self.thread_lock.release()

    def stop_thread(self):
        self.thread_lock = None

    def run_thread(self, dt, post_step_callback, sleep_fun):
        record_foot_traj = True
        while self.thread_lock:
            with self.thread_lock:
                post_step_callback()
                phase_duration = \
                    self.ds_duration if self.cur_stance.is_double_support \
                    else self.ss_duration
                self.rem_time = phase_duration
                for t in arange(0., phase_duration, dt):
                    self.state_time = t
                    if self.cur_stance.is_single_support:
                        progress = self.state_time / phase_duration  # in [0, 1]
                        prev_pos = self.free_foot.p
                        self.free_foot.update_pose(progress)
                        if record_foot_traj:
                            if self.cur_stance.left_foot:
                                self.right_foot_traj_handles.append(
                                    draw_line(prev_pos, self.free_foot.p,
                                              color='r', linewidth=3))
                            else:
                                self.left_foot_traj_handles.append(
                                    draw_line(prev_pos, self.free_foot.p,
                                              color='g', linewidth=3))
                    sleep_fun(dt)
                    self.rem_time -= dt
                if self.cur_stance.is_double_support:
                    next_stance = self.next_stance

                    def is_inside_next_com_polygon(p):
                        return next_stance.is_inside_static_equ_polygon(p, 39.)

                    while not is_inside_next_com_polygon(self.com.p):
                        sleep_fun(dt)
                self.step()

    @property
    def next_contact(self):
        return self.contacts[self.next_contact_id]

    @property
    def next_duration(self):
        if self.next_state.startswith('SS'):
            return self.ss_duration
        return self.ds_duration

    @property
    def next_stance(self):
        if self.next_state == 'SS-L':
            left_foot = self.cur_stance.left_foot
            right_foot = None
        elif self.next_state == 'DS-R':
            left_foot = self.cur_stance.left_foot
            right_foot = self.next_contact
        elif self.next_state == 'SS-R':
            left_foot = None
            right_foot = self.cur_stance.right_foot
        elif self.next_state == 'DS-L':
            left_foot = self.next_contact
            right_foot = self.cur_stance.right_foot
        else:  # should not happen
            assert False, "Unknown state: %s" % self.next_state
        return Stance(self.next_state, left_foot, right_foot)

    @property
    def next_ss_stance(self):
        assert self.cur_stance.is_single_support
        t = self.transitions
        if self.cur_stance.left_foot is None:
            return Stance(t[t[self.state]], self.next_contact, None)
        else:  # self.cur_stance.right_foot is None
            return Stance(t[t[self.state]], None, self.next_contact)

    @property
    def next_state(self):
        return self.transitions[self.state]

    def get_time_to_transition(self):
        return self.rem_time

    def step(self):
        next_stance = self.next_stance
        next_state = self.next_state
        if next_state.startswith('DS'):
            self.next_contact_id += 1
            if self.next_contact_id >= self.nb_contacts:
                if self.cyclic:
                    self.next_contact_id -= self.nb_contacts
                elif self.thread_lock:  # thread is running
                    self.stop_thread()
        self.cur_stance = next_stance
        self.state = next_state

    def get_preview_targets(self):
        time_to_transition = self.rem_time
        if self.cur_stance.is_single_support \
                and time_to_transition < 0.5 * self.ss_duration:
            horizon = time_to_transition \
                + self.ds_duration \
                + 0.5 * self.ss_duration
            com_target = self.next_ss_stance.com
        elif self.cur_stance.is_double_support:
            horizon = time_to_transition + 0.5 * self.ss_duration
            com_target = self.cur_stance.com
        else:  # single support with plenty of time ahead
            horizon = time_to_transition
            com_target = self.cur_stance.com
        return (horizon, com_target)
