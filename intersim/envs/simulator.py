# simulator.py

from enum import Enum

import torch
import numpy as np
import pickle

from intersim.utils import ssdot_to_simstates, to_circle, get_map_path, get_svt, powerseries, horner_scheme
from intersim import Box, StackedVehicleTraj, InteractionGraph
from intersim.viz import animate, build_map
from intersim.collisions import check_collisions, check_collisions_trajectory

import gym
from gym import error, spaces, utils
from gym.utils import seeding

import logging
logger = logging.getLogger(__name__)

from typing import Callable

class RewardMethod(Enum):
    """
    NONE: 0 reward throughout
    CUSTOM: user-specified reward function R(next_state, action)
    """
    NONE = 'none'
    CUSTOM = 'custom'

class ObserveMethod(Enum):
    """
    LIGHT: simple state and graph information
    FULL: simple and relative state information, graph information, and map information
    HIDDEN: FULL state information and hidden environment information

    """
    LIGHT = 'light'
    FULL = 'full'
    HIDDEN = 'hidden' 

class InteractionSimulator(gym.Env):
    
    metadata = {'render.modes': ['live','file','post']}

    def __init__(self, 
                 loc: int = 0,
                 track: int = 0,
                 svt: StackedVehicleTraj = None, 
                 map_path: str = None, 
                 graph: InteractionGraph = None,
                 min_acc: float=-10.0, max_acc: float=10.0,
                 stop_on_collision: bool=False,
                 check_collisions: bool=False,
                 reward_method='none', observe_method='full',
                 custom_reward: Callable[[dict, torch.Tensor], float] = lambda x, y: 0.,
                 shuffle_tracks: bool = False, shuffle_tracks_seed: int = 0,
                 mask_relstate: bool=False,
                 ):
        """
        Roundabout simulator.
        Args:
            loc (int): location index of scene
            track (into): track number of scene
            svt (StackedVehicleTraj): vehicle trajectory to reference
            map_path (str): path to map .osm file
            graph (InteractionGraph): graph depicting vehicle interactions
            min_acc (float): minimum acceleration allowed
            max_acc (float): maximum acceleration allowed
            stop_on_collision (bool): whether to stop the episode on collision
            reward_method (str): reward type. see RewardMethod class
            observe_method (str): observation type. see ObserveMethod class
            custom_reward (Callable): custom reward function R(s',a) to be used w/ 'custom' reward method
            shuffle_tracks (bool): whether to shuffle to vehicle tracks
            shuffle_tracks_seed (int): seed to use when shuffling the tracks
            mask_relstate (bool): whether to mask vehicles based on the interaction graph
        Notes:
            action_space is [min_acc,max_acc] ^ nvehicles
            state_space is [x,y,v,psi,psidot] ^ nvehicles
        """

        # Get stacked vehicle trajectory
        if not svt:
            svt, filename = get_svt(loc, track)
            logging.info('Vehicle Trajectory Paths: {}'.format(filename))
        else:
            logging.info('Custom Vehicle Trajectory Paths')
            
        # Get map path
        if not map_path:
            map_path = get_map_path(loc)
        logging.info('Map Path: {}'.format(map_path))
        
        # shuffle tracks
        self._shuffle_tracks = shuffle_tracks
        self._shuffle_tracks_seed = shuffle_tracks_seed
        if self._shuffle_tracks:
            svt = svt.shuffle_tracks(seed = shuffle_tracks_seed)

        # simulator fields 
        self._nv = svt.nv
        self._dt = svt.dt
        self._svt = svt
        self._state = torch.zeros(self._nv, 2) * np.nan
        self._ind = 0
        self._exceeded = torch.tensor([False] * self._nv)
        self._stop_on_collision = stop_on_collision
        self._check_collisions = check_collisions
        self._min_acc = min_acc
        self._max_acc = max_acc
        self._map_path = map_path
        self._map_info = self.extract_map_info()
        self._xpoly = self._svt.xpoly
        self._ypoly = self._svt.ypoly
        self._lengths = self._svt.lengths
        self._widths = self._svt.widths
        self._graph = InteractionGraph() if graph is None else graph
        self._graph.update_graph(self.projected_state)
        self._reward_method = RewardMethod(reward_method)
        self._observe_method = ObserveMethod(observe_method)
        self._custom_reward = custom_reward
        self._mask_relstate = mask_relstate

        # gym fields
        # using intersim.Box over spaces.Box for pytorch
        self.action_space = Box(low=min_acc, high=max_acc, shape=(self._nv,1))
        low = torch.tensor([[-np.inf,-np.inf,0.,-np.pi, -np.inf]]).expand(self._nv,5)
        high = torch.tensor([[np.inf,np.inf,np.inf,np.pi, np.inf]]).expand(self._nv,5)
        self.state_space = Box(low=low, high=high, shape=(self._nv,5))
        self.done= False
        self.info = {
            'raw_state': self._state.clone(),
            'xpoly': self._xpoly,
            'dxpoly': self._svt.dxpoly,
            'ddxpoly': self._svt.ddxpoly,
            'ypoly': self._ypoly,
            'dypoly': self._svt.dypoly,
            'ddypoly': self._svt.ddypoly,
            'smax': self._svt.smax,
            'lengths': self._lengths,
            'widths': self._widths,
        }

        # rendering fields
        self._state_list = []
        self._graph_list = []

        super(InteractionSimulator, self).__init__() # is this necessary??

    @property
    def t(self):
        """
        Returns time
        Returns:
            t (float): simulation time
        """
        return self._svt.minT + self._dt * self._ind

    @property
    def state(self):
        """
        Return [s, v]
        Returns:
            state (torch.tensor): (nv, 2) state in terms of s, sdot
        """
        return self._state
    
    @property
    def path_coefs(self):
        """
        Return coefficients for computing x(s) and y(s)
        Returns:
            xpoly (torch.tensor): (nv, deg) x polynomial path coefficients
            ypoly (torch.tensor): (nv, deg) y polynomial path coefficients
        """
        svt = self._svt
        return svt.xpoly, svt.ypoly
    
    @property
    def smax(self):
        """
        Return maximum path lengths
        Returns:
            smax (torch.tensor): (nv,) maximum path lengths
        """
        return self._svt.smax
    
    @property
    def projected_state(self):
        """
        Projects [s, v] to [x,y,v,psi,psidot]
        Returns:
            projstate (torch.tensor): (nv, 5) projected state
        """
        return self._project_state(self._state)

    def _project_state(self, state):
        """
        Project a state [s, v] to [x,y,v,psi,psidot]
        Args:
            state (torch.Tensor): (nv, 2) raw state
        Returns:
            projstate (torch.tensor): (nv, 5) projected state
        """
        svt = self._svt
        projstate = ssdot_to_simstates(state[:,0].unsqueeze(0), state[:,1].unsqueeze(0),
                svt.xpoly, svt.dxpoly, svt.ddxpoly,
                                svt.ypoly, svt.dypoly, svt.ddypoly)
        projstate = projstate[0]
        # remap psi to [-pi, pi)
        projstate[...,3] = to_circle(projstate[...,3])
        return projstate
    
    def _project_multiple_states(self, state):
        """
        Project a sequence of states [s, v] to [x,y,v,psi,psidot]
        Args:
            state (torch.Tensor): (T, nv, 2) raw states
        Returns:
            projstate (torch.tensor): (T, nv, 5) projected states
        """
        svt = self._svt
        projstate = ssdot_to_simstates(
            state[:, :,0], state[:, :,1],
            svt.xpoly, svt.dxpoly, svt.ddxpoly,
            svt.ypoly, svt.dypoly, svt.ddypoly
        )

        # remap psi to [-pi, pi)
        projstate[...,3] = to_circle(projstate[...,3])
        return projstate

    @property
    def relative_state(self):
        return self._relative_state(self.projected_state)

    def _relative_state(self, projstate):
        """
        Compute relative state information from [x,y,v,psi,psidot] projected state
        Args:
            projstate (torch.tensor): (nv,5) projected state
        Returns:
            relstate (torch.tensor): (nv, nv, 6) projected state, where relstate[i,j,...] = projstate[j,...] - projstate[i,...]
                The velocity is split up to vx and vy to compute relative velocities in a reasonable way
        """
        # First separate v into vx and vy
        psi = projstate[..., 3:4]
        v = projstate[..., 2:3]
        vx = v * psi.cos()
        vy = v * psi.sin()
        projstate = torch.cat([projstate[..., 0:2], vx, vy, projstate[..., 3:5]], dim=-1)

        relstate = projstate.unsqueeze(0) - projstate.unsqueeze(1)
        # remap psi to [-pi, pi)
        relstate[...,4] = to_circle(relstate[...,4])
        # fill diagonal with nan instead of zeros
        torch.diagonal(relstate).fill_(np.nan)

        if self._mask_relstate:
            relstate[~self._graph.adjacency_matrix(self._nv)] = np.nan

        return relstate
    
    def _generate_paths(self, delta: float = 10., n: int = 20, is_distance: bool=True, override: bool=False):
        """
        Return the upcoming path of all vehicles in fixed path-length increments up to maximum path.
        Args:
            delta (float): path increment
            is_distance (bool): whether delta denotes m (True) or s (False)
            n (int): number of increments to calculate
            override (bool): whether to override calculation to do equal segments along valid path
        Returns:
            x (torch.Tensor): (nv, n) x positions
            y (torch.Tensor): (nv, n) y positions
        """

        if override:
            delta = (self._svt.smax.unsqueeze(1) - self._state[:,0:1]) / n * (n-1) / n
            ds = delta * torch.arange(1,n+1).repeat(self._nv,1)
        elif is_distance:
            ds = delta * torch.arange(1,n+1).repeat(self._nv,1)
        else:
            v = self._state[:,1:2]
            ds = delta * v * torch.arange(1,n+1).repeat(self._nv,1)

        s = (ds + self._state[:,0:1]).type(torch.float64)
        smax = self.smax.type(torch.float64).unsqueeze(1)
        nni = (s <= smax)

        x = horner_scheme(s, self._xpoly)
        y = horner_scheme(s, self._ypoly)

        x_max = horner_scheme(smax, self._xpoly)
        y_max = horner_scheme(smax, self._ypoly)

        dx = horner_scheme(smax, self._svt._dxpoly)
        dy = horner_scheme(smax, self._svt._dypoly)
        x_line = x_max + (s - smax) * dx
        y_line = y_max + (s - smax) * dy

        x[~nni] = x_line[~nni]
        y[~nni] = y_line[~nni]
        return x, y

    @property
    def map_info(self):
        """
        Return information about environment map
        Returns:
            map_info: information about map
        """
        return self._map_info
    
    def extract_map_info(self):
        """
        Generates information about environment map
        Returns:
            map_info: information about map
        """
        map_info, _ = build_map(self._map_path)
        return map_info

    def _next_state(self, state, action):
        """
        Propagate the state forward a single step
        Args:
            state (torch.Tensor): (nv, 2) environment state (s, sdot)
            action (torch.Tensor): (nv, 1) action (acceleration)
        Returns:
            next_state (torch.Tensor): (nv, 2) next environment state
            action (torch.Tensor): (nv, 1) true action taken (acceleration)
            exceeded (torch.Tensor): (nv,) bool tensor of cars which exceeded their track
        """
        # check for proper action input
        # 1) make sure all non-nan states have actions
        # 2) set actions for all nan state to 0
        # 3) make sure final action is inside action space, if not clamp
        
        nns = ~torch.isnan(state[:,0]) 
        nna = ~torch.isnan(action[:,0]) 
        nns_na = nns & ~nna
        if torch.any(nns_na):
            raise Exception('Time: {}. Some cars receive nan actions'.format(self.t))
        action[~nna] = 0.
        if not self.action_space.contains(action):
            logging.info('Time: {}. Requested action outside of bounds, being clamped'.format(self.t))
            action = action.clamp(self._min_acc, self._max_acc)

        # euler step the state
        nextv = state[:,1:2] + action * self._dt
        nextv = nextv.clamp(0., np.inf) # not differentiable!
        next_state = torch.zeros_like(state)
        next_state[:,0:1] = state[:,0:1] + 0.5*self._dt*(nextv + state[:,1:2])
        next_state[:,1:2] = nextv

        # see which states exceeded their maxes
        exceeded = (next_state[:,0] > self._svt.smax)
        next_state[exceeded] = np.nan

        return next_state, action, exceeded

    def _next_state_batch(self, state, action):
        """
        Propagate the state forward a single step
        Args:
            state (torch.Tensor): (..., nv, 2) environment state (s, sdot)
            action (torch.Tensor): (..., nv, 1) action (acceleration)
        Returns:
            next_state (torch.Tensor): (..., nv, 2) next environment state
            action (torch.Tensor): (..., nv, 1) true action taken (acceleration)
            exceeded (torch.Tensor): (..., nv,) bool tensor of cars which exceeded their track
        """
        if not self.action_space.contains(action):
            logging.info('Time: {}. Requested action outside of bounds, being clamped'.format(self.t))

        action = action.clamp(self._min_acc, self._max_acc)

        # euler step the state
        nextv = state[...,1:2] + action * self._dt
        nextv = nextv.clamp(0., np.inf) # not differentiable!
        next_state = torch.zeros_like(state)
        next_state[...,0:1] = state[...,0:1] + 0.5*self._dt*(nextv + state[...,1:2])
        next_state[...,1:2] = nextv

        # see which states exceeded their maxes
        exceeded = (next_state[...,0] > self._svt.smax)
        next_state[exceeded] = np.nan

        return next_state, action, exceeded
    
    def step(self, action):
        """
        Step simulation forward 1 time-step.
        Args:
            action (torch.tensor): (nvehicles,1) accelerations
        Returns:
            observation (dict): observation dictionary
            reward (float): reward
            episode_over (bool): whether the episode has ended
            info (dict): diagnostic information useful for debugging
        """
        assert not self.done, "Simulation has ended and must be reset"
        action = action.clone()
        prev_state = self.projected_state.clone()
        self._ind += 1
        nns = ~torch.isnan(self._state[:,0])
        next_state, action, exceeded = self._next_state(self._state.clone(), action)
        self._state = next_state
        self._exceeded = self._exceeded | exceeded
        self.done = torch.all(self._exceeded)

        # see which new vehicles should spawn
        
        # TODO: add an isFree checker 
        free = torch.tensor([True] * self._nv)
        should_spawn = (self._svt.t0 <= self.t) & ~self._exceeded & ~nns & free

        # for now, just spawn them
        spawned = should_spawn
        self._state[spawned,0] = self._svt.s0[spawned]
        self._state[spawned,1] = self._svt.v0[spawned]
        
        projstate = self.projected_state

        self.info.update({'collision': False})
        # check for collisions if necessary, terminate if stopping on collisions
        if self._stop_on_collision or self._check_collisions:
            collision = check_collisions(projstate, self._lengths, self._widths)
            self.info.update({'collision': collision})
            if collision and self._stop_on_collision:
                self.done = True

        # update interaction graph
        self._graph.update_graph(projstate)

        # generate info
        self.info.update({
            'exceeded': np.argwhere(self._exceeded.detach().numpy()).flatten(),
            'should_spawn': np.argwhere(should_spawn.detach().numpy()).flatten(),
            'spawned': np.argwhere(spawned.detach().numpy()).flatten(),
            'raw_state': self._state.clone(),
            'projected_state': self.projected_state.clone(),
            'prev_state': prev_state,
            'action_taken': action.clone()
        })
        self._last_action = action.clone()

        # generate observation
        ob = self._get_observation(projstate)
        
        # generate reward from new state and action
        reward = self._get_reward(projstate, action)
        return ob, reward, self.done, self.info   
    
    def check_future_collisions(self, actions):
        """
        Check whether there are collisions in future trajectories by propagating forward actions
        Args:
            actions (list of torch.Tensor): list of B (T, nv, adims) T-length action profiles
        Returns:
            collisions (torch.Tensor): (B,) booleans for whether propagating actions will lead to a collision
        """
        B = len(actions)
        states = self.propagate_action_profile(actions)
        collisions = torch.zeros(B, dtype=torch.bool)
        for iB in range(B):
            collisions[iB] = torch.any(check_collisions_trajectory(states[iB], self._lengths, self._widths))
        return collisions

    def propagate_action_profile(self, actions):
        """
        Take an joint action profile and propagate the current environment appropriately
        Args:
            actions (list of torch.Tensor): list of B (T, nv, adims) T-length action profiles
        Returns:
            states (list of torch.Tensor): list of B (T, nv, sdims) resulting projected states
        """
        states = []
        for action in actions:
            assert action.ndim == 3, 'Improper action dimension'
            T, nv, adims = action.shape
            x = torch.zeros(T, nv, 5)
            state = self._state.clone()
            for iT in range(T):
                # propagate one step
                state, _, _ =  self._next_state(state, action[iT])
                x[iT] = self._project_state(state)
            states.append(x)
        
        return states
    
    def propagate_action_profile_vectorized(self, actions):
        """
        Take an joint action profile and propagate the current environment appropriately
        Args:
            actions (torch.Tensor): (B, T, nv, adims) T-length action profiles
        Returns:
            states (torch.Tensor): (B, T, nv, sdims) resulting projected states
        """
        B, T, nv, adims = actions.shape
        _, arcsdims = self._state.shape

        states = self._state.repeat(B, T+1, 1, 1)
        assert states.shape == (B, T+1, nv, arcsdims)
        
        states = states.transpose(0, 1)
        actions = actions.transpose(0, 1)
        assert states.shape == (T+1, B, nv, arcsdims)
        assert actions.shape == (T, B, nv, adims)

        for iT in range(T):
            # propagate one step
            s, _, _ =  self._next_state_batch(states[iT], actions[iT])
            states[iT + 1] = s

        states = states.transpose(0, 1)
        states = states[:, 1:, :, :]
        assert states.shape == (B, T, nv, arcsdims)

        states = self._project_multiple_states(states.reshape(-1, nv, arcsdims))
        states = states.reshape(B, T, nv, 5)

        return states

    def target_state(self, ssdot, mu=0.):
        """
        Take an action to target a particular next state
        Args:
            ssdot (torch.Tensor): (nv, 2) tensor of next s and sdot to target
            mu (float): regularizing coefficient on next s to target (to smooth accelerations)
        Returns:
            action (torch.Tensor): (nv, 1) tensor of ideal action to take
        """
        assert len(ssdot) == self._nv, 'Incorrect target state size'
        next_s = ssdot[:,0:1]
        s = self._state[:,0:1]
        v = self._state[:,1:2]
        action = 2 * (next_s - s - (self._dt * v)) / (self._dt**2 + 4 * mu / self._dt**2)
        nan_mask = torch.isnan(s) | torch.isnan(next_s)
        action[nan_mask] = 0.
        return action

    def _get_observation(self, next_state):
        """
        Generate observation
        Args:
            next_state (torch.tensor): (nv,5) projected next state
        Returns:
            observation (dict): observation with following potential keys
                state (torch.tensor): (nv,5) projected next state
                neighbor_dict (dict): interaction graph neighbor dictionary
                relative_state (torch.tensor): (nv,nv,5) relative state information where [i,j,k] encodes state[j,k] - state[i,k]
                map_info: map information embedding
                paths (tuple): tuple of x and y positions of n future points along each trajectory
                    x (torch.tensor): (nv,n) x positions of n future points
                    y (torch.tensor): (nv,n) y positions of n future points
                hidden_info: external information that would usually stay hidden for training
        """
        observation = {}
        observation['state'] = next_state
        observation['neighbor_dict'] = self._graph.neighbor_dict
        if self._observe_method in [ObserveMethod.FULL, ObserveMethod.HIDDEN]:
            observation['relative_state'] = self._relative_state(next_state)
            observation['paths'] = self._generate_paths(delta=10., n=20, is_distance=True)
            observation['map_info'] = self.map_info
        if self._observe_method in [ObserveMethod.HIDDEN]:
            observation['hidden_info'] = self.info
        return observation
    
    def _get_reward(self, next_state, action) -> float:
        """
        Generate reward
        Args:
            next_state (torch.tensor): (nv,5) next projected state
            action (torch.tensor): (nv,) action taken
        Returns:
            reward (float): immediate reward
        """
        if self._reward_method == RewardMethod.NONE:
            return 0.
        elif self._reward_method == RewardMethod.CUSTOM:
            return self._custom_reward(next_state, action)
        else:
            raise ValueError('Invalid reward method')

    def reset(self):
        """
        Reset simulation.
        Returns:
            observation (dict): observation dictionary
            info (dict): diagnostic information useful for debugging
        """

        self._state = self._svt.state0.clone()
        self._ind = 0
        self._exceeded = torch.tensor([False] * self._nv)
        self._graph.update_graph(self.projected_state)
        self.done = False

        # rendering fields
        self._state_list = []
        self._graph_list = []
        self._action_list = []
        self._last_action = None # for saving joint actions
        logging.info('Environment Reset')

        # generate info
        self.info.update({
            'raw_state': self._state.clone(),
        })

        # generate observation
        ob = self._get_observation(self.projected_state)
        
        return ob, self.info   

    def render(self, mode='post'):
        """
        Render environment
        Args:
            mode (str): different rendering options
                live: render a live matplotlib
                file: save necessary information to file
                post: save necessary information to file, and generate video upon closing
            filestr (str): base file string to save states, graphs, and videos to
        """
        self._mode = mode # latest mode

        if mode == 'live':
            import matplotlib
            import matplotlib.pyplot as plt
            raise NotImplementedError
        if mode in ['file', 'post']:
            self._state_list.append(self.projected_state)
            self._graph_list.append(self._graph.edges)
            if self._last_action is not None:
                self._action_list.append(self._last_action)

    def close(self, **kwargs):
        """
        Clean up the environment
        Args:
            filestr (str): base file string to save states, graphs, and videos to
        """ 
        if self._mode == 'live':
            import matplotlib
            import matplotlib.pyplot as plt
            plt.close()
        if self._mode in ['file', 'post']:
            filestr = kwargs.get('filestr', 'render')
            stacked_states = torch.stack(self._state_list)
            stacked_actions = torch.stack(self._action_list)
            torch.save(stacked_states, filestr+'_states.pt')
            torch.save(stacked_actions, filestr+'_actions.pt')
            pickle.dump(self._graph_list,open(filestr+'_graphs.pkl', 'wb'))
            torch.save(self._lengths, filestr+'_lengths.pt')
            torch.save(self._widths, filestr+'_widths.pt')
            torch.save(self._xpoly, filestr+'_xpoly.pt')
            torch.save(self._ypoly, filestr+'_ypoly.pt')
            if self._mode == 'post':
                animate(self._map_path, stacked_states, self._lengths, self._widths, 
                        graphs=self._graph_list, **kwargs)



