sandbox/haoran/deep_q_rl/deep_q_rl/ale_data_set.py"""The ALEExperiment class handles the logic for training a deep
Q-learning agent in the Arcade Learning Environment.

Author: Nathan Sprague

"""
from rllab.misc import logger
import numpy as np
import cv2
import time
import sys, os
from .ale_python_interface.ale_python_interface import ALEInterface
import atari_py

# Number of rows to crop off the bottom of the (downsampled) screen.
# This is appropriate for breakout, but it may need to be modified
# for other games.
CROP_OFFSET = 8


class ALEExperiment(object):
    def __init__(self, ale_args, agent, resized_width, resized_height,
                 resize_method, num_epochs, epoch_length, test_length,
                 frame_skip, death_ends_episode, max_start_nullops,
                 length_in_episodes=False, max_episode_length=np.inf, game='', observation_type="image", record_image=True, record_ram=False,
                 record_rgb_image=False,
                 recorded_rgb_image_scale=1.,
                 ):
        self.ale_args = ale_args
        ale = ALEInterface()
        ale.setInt(b'random_seed', ale_args["seed"])

        if ale_args["plot"]:
            if sys.platform == 'darwin':
                import pygame
                pygame.init()
                ale.setBool('sound', False) # Sound doesn't work on OSX

        ale.setBool(b'display_screen', ale_args["plot"])
        ale.setFloat(b'repeat_action_probability',
                     ale_args["repeat_action_probability"])
        rom = ale_args["rom_path"]
        rom = atari_py.get_game_path(game)
        if not os.path.exists(rom):
            print("Rom file %s does not exist."%(rom))
            sys.exit(1)
        # ale.loadROM(rom)
        ale.loadROM(str.encode(rom))
        self.ale = ale

        self.agent = agent
        self.num_epochs = num_epochs
        self.epoch_length = epoch_length
        self.test_length = test_length
        self.frame_skip = frame_skip
        self.death_ends_episode = death_ends_episode
        self.min_action_set = ale.getMinimalActionSet()
        self.resized_width = resized_width
        self.resized_height = resized_height
        self.resize_method = resize_method
        self.width, self.height = ale.getScreenDims()

        self.buffer_length = 2
        self.buffer_count = 0
        self.screen_buffer = np.empty((self.buffer_length,
                                       self.height, self.width),
                                      dtype=np.uint8)

        self.terminal_lol = False # Most recent episode ended on a loss of life
        self.max_start_nullops = max_start_nullops

        # Whether the lengths (test_length and epoch_length) are specified in
        # episodes. This is mainly for testing
        self.length_in_episodes = length_in_episodes
        self.max_episode_length = max_episode_length

        # allows using RAM state for state counting or even q-learning
        assert observation_type in ["image","ram"]
        if observation_type == "image":
            assert record_image
        elif observation_type == "ram":
            assert record_ram
        self.observation_type = observation_type
        self.record_image = record_image
        self.record_ram = record_ram
        if record_ram:
            self.ram_state = np.zeros(ale.getRAMSize(), dtype=np.uint8)
        self.record_rgb_image = record_rgb_image
        self.recorded_rgb_image_scale = recorded_rgb_image_scale

    def run(self):
        """
        Run the desired number of training epochs, a testing epoch
        is conducted after each training epoch.
        """
        for epoch in range(1, self.num_epochs + 1):
            self.agent.start_epoch(epoch,phase="Train")
            self.run_epoch(epoch, self.epoch_length)
            self.agent.finish_epoch(epoch,phase="Train")

            if self.test_length > 0:
                self.agent.start_testing(epoch)
                self.agent.start_epoch(epoch,phase="Test")
                self.run_epoch(epoch, self.test_length, True)
                self.agent.finish_epoch(epoch,phase="Test")
                self.agent.finish_testing(epoch)
            logger.dump_tabular(with_prefix=False)
        self.agent.cleanup()

    def run_epoch(self, epoch, num_steps, testing=False):
        """ Run one 'epoch' of training or testing, where an epoch is defined
        by the number of steps executed.  Prints a progress report after
        every trial

        Arguments:
        epoch - the current epoch number
        num_steps - steps per epoch
        testing - True if this Epoch is used for testing and not training

        """
        phase = "Test" if testing else "Train"
        self.terminal_lol = False # Make sure each epoch starts with a reset.
        steps_left = num_steps
        start_time = time.clock()
        episode_count = 0
        episode_reward_list = []
        episode_length_list = []
        while steps_left > 0:
            max_steps = np.amin([steps_left, self.max_episode_length])
            _, episode_length, episode_reward = self.run_episode(max_steps, testing)
            episode_reward_list.append(episode_reward)
            episode_length_list.append(episode_length)
            total_time = time.clock() - start_time
            episode_count += 1
            logger.log("""
                {phase} epoch: {epoch_count}, steps left: {steps_left}, total time: {total_time},
                episode: {episode_count}, episode length: {episode_length}, episode reward: {episode_reward},
                """.format(
                phase=phase,
                epoch_count=epoch,
                steps_left=steps_left,
                total_time="%.0f secs"%(total_time),
                episode_count=episode_count,
                episode_length=episode_length,
                episode_reward=episode_reward,
                ))
            steps_left -= episode_length

        # logging
        if phase == "Train":
            logger.record_tabular("Epoch",epoch)
        logger.record_tabular("%sEpochTime"%(phase),"%.0f"%(total_time))
        logger.record_tabular("%sAverageReturn"%(phase), np.average(episode_reward_list))
        logger.record_tabular("%sStdReturn"%(phase), np.std(episode_reward_list))
        logger.record_tabular("%sMedianReturn"%(phase), np.median(episode_reward_list))
        logger.record_tabular("%sMaxReturn"%(phase), np.amax(episode_reward_list))
        logger.record_tabular("%sMinReturn"%(phase), np.amin(episode_reward_list))

        logger.record_tabular("%sAverageEpisodeLength"%(phase), np.average(episode_length_list))
        logger.record_tabular("%sStdEpisodeLength"%(phase), np.std(episode_length_list))
        logger.record_tabular("%sMedianEpisodeLength"%(phase), np.median(episode_length_list))
        logger.record_tabular("%sMaxEpisodeLength"%(phase), np.amax(episode_length_list))
        logger.record_tabular("%sMinEpisodeLength"%(phase), np.amin(episode_length_list))

        # save iteration parameters
        logger.log("Saving iteration parameters...")
        params = dict(
            epoch=epoch,
            agent=self.agent,
        )
        logger.save_itr_params(epoch,params)


    def _init_episode(self):
        """ This method resets the game if needed, performs enough null
        actions to ensure that the screen buffer is ready and optionally
        performs a randomly determined number of null action to randomize
        the initial game state."""
        if not self.terminal_lol or self.ale.game_over():
            self.ale.reset_game()

            if self.max_start_nullops > 0:
                random_actions = np.random.randint(0, self.max_start_nullops+1)
                for _ in range(random_actions):
                    self._act(0) # Null action

        # Make sure the screen buffer is filled at the beginning of
        # each episode...
        self._act(0)
        self._act(0)


    def _act(self, action):
        """Perform the indicated action for a single frame, return the
        resulting reward and store the resulting screen image in the
        buffer

        """
        reward = self.ale.act(action)

        if self.record_image:
            # replace the current buffer image by the current screen
            index = self.buffer_count % self.buffer_length
            self.ale.getScreenGrayscale(self.screen_buffer[index, ...])

        if self.record_ram:
            self.ale.getRAM(self.ram_state)

        # count the total number of images seen
        self.buffer_count += 1
        return reward

    def _step(self, action):
        """ Repeat one action the appopriate number of times and return
        the summed reward. """
        reward = 0
        for _ in range(self.frame_skip):
            reward += self._act(action)

        env_info = {}
        if self.record_ram:
            env_info["ram_state"] = self.ale.getRAM()
        if self.record_rgb_image:
            rgb_img = self.ale.getScreenRGB()
            scale = self.recorded_rgb_image_scale
            if abs(scale-1.0) > 1e-4:
                rgb_img = cv2.resize(rgb_img, dsize=(0,0),fx=scale,fy=scale)
            env_info["rgb_image"] = rgb_img

        return reward, env_info

    def run_episode(self, max_steps, testing):
        """Run a single training episode.

        The boolean terminal value returned indicates whether the
        episode ended because the game ended or the agent died (True)
        or because the maximum number of steps was reached (False).
        Currently this value will be ignored.

        Return: (terminal, num_steps)

        """

        self._init_episode()

        start_lives = self.ale.lives()

        action = self.agent.start_episode(self.get_observation())
        num_steps = 0
        total_reward = 0
        while True:
            reward, env_info = self._step(self.min_action_set[action])
            total_reward += reward
            self.terminal_lol = (self.death_ends_episode and not testing and
                                 self.ale.lives() < start_lives)
            terminal = self.ale.game_over() or self.terminal_lol
            num_steps += 1

            if terminal or num_steps >= max_steps and not self.length_in_episodes:
                self.agent.end_episode(reward, terminal, env_info)
                break

            action = self.agent.step(reward, self.get_observation(),
                env_info)

        # if the lengths are in episodes, this episode counts as 1 "step"
        if self.length_in_episodes:
            return terminal, 1, total_reward
        else:
            return terminal, num_steps, total_reward


    def get_observation(self):
        """ Resize and merge the previous two screen images """

        if self.observation_type == "image":
            assert self.record_image
            assert self.buffer_count >= 2
            index = self.buffer_count % self.buffer_length - 1
            max_image = np.maximum(self.screen_buffer[index, ...],
                                   self.screen_buffer[index - 1, ...])
            return self.resize_image(max_image)
        else:
            assert self.record_ram
            ram_size = len(self.ram_state)
            # reshape the ram state to make it like a (width x height) image
            ram = np.reshape(self.ram_state, (1,ram_size))
            return ram

    def resize_image(self, image):
        """ Appropriately resize a single image """

        if self.resize_method == 'crop':
            # resize keeping aspect ratio
            resize_height = int(round(
                float(self.height) * self.resized_width / self.width))

            resized = cv2.resize(image,
                                 (self.resized_width, resize_height),
                                 interpolation=cv2.INTER_LINEAR)

            # Crop the part we want
            crop_y_cutoff = resize_height - CROP_OFFSET - self.resized_height
            cropped = resized[crop_y_cutoff:
                              crop_y_cutoff + self.resized_height, :]

            return cropped
        elif self.resize_method == 'scale':
            return cv2.resize(image,
                              (self.resized_width, self.resized_height),
                              interpolation=cv2.INTER_LINEAR)
        elif self.resize_method == 'none':
            return image
        else:
            raise ValueError('Unrecognized image resize method.')
