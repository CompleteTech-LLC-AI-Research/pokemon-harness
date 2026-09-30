#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
__pdoc__ = {
    "GameWrapperPokemonPinball.cartridge_title": False,
    "GameWrapperPokemonPinball.post_tick": False,
    "GameWrapperPokemonPinball._set_stage": False,
    "GameWrapperPokemonPinball.ball_size": False,
}

import logging
from enum import Enum

from pyboy.utils import PyBoyException, WindowEvent, bcd_to_dec

from .base_plugin import PyBoyGameWrapper
from .game_wrapper_pokemon_pinball_data import *

logger = logging.getLogger(__name__)


class Stage(Enum):
    """
    The stage values in the game.
    """

    RED_TOP = 0
    RED_BOTTOM = 1
    BLUE_TOP = 4
    BLUE_BOTTOM = 5
    GENGAR = 7
    MEWTWO = 9
    MEOWTH = 11
    DIGLETT = 13
    SEEL = 15


class SpecialMode(Enum):
    CATCH = 0
    EVOLVE = 1
    STAGE_CHANGE = 2


class BallType(Enum):
    POKEBALL = 0
    GREATBALL = 2
    ULTRABALL = 3
    MASTERBALL = 5


class BallSize(Enum):
    DEFAULT = 0
    MINI = 1
    SUPERMINI = 2


RedStages = [Stage.RED_TOP, Stage.RED_BOTTOM]
BlueStages = [Stage.BLUE_TOP, Stage.BLUE_BOTTOM]

RedBonusStages = [Stage.DIGLETT, Stage.GENGAR, Stage.MEWTWO]

BlueBonusStages = [Stage.MEOWTH, Stage.SEEL, Stage.MEWTWO]

AllBonusStageValues = RedBonusStages + BlueBonusStages


class GameWrapperPokemonPinball(PyBoyGameWrapper):
    """
    This class wraps Pokemon Pinball, and provides access to game info for AIs.
    """

    cartridge_title = "POKEPINBALLVPH"

    def __init__(self, *args, **kwargs):
        self.shape = (20, 16)
        """The shape of the game area"""
        self.score = 0
        """The score provided by the game"""
        self.balls_left = 0
        """The lives remaining provided by the game"""
        self.game_over = False
        """The game over state"""
        self.ball_type = BallType.POKEBALL.value
        """The current ball type"""
        self.multiplier = 1
        """The current multiplier"""
        self.current_stage = 0
        """The current stage"""
        self.ball_size = 0
        self.ball_saver_seconds_left = 0
        """The current ball saver seconds left"""
        self.pokedex = [False] * 151
        self._unlimited_saver = False
        self.ball_x = 0
        """The x position of the ball"""
        self.ball_y = 0
        """The y position of the ball"""
        self.ball_x_velocity = 0
        """The x velocity of the ball"""
        self.ball_y_velocity = 0
        """The y velocity of the ball"""
        self.special_mode = 0
        """
        The special mode state value


        Example:
        ```python
        >>> from pyboy.plugins.game_wrapper_pokemon_pinball import SpecialMode
        >>> pyboy = PyBoy(pokemon_pinball_rom)
        >>> pyboy.game_wrapper.special_mode == SpecialMode.CATCH.value
        True
        ```
        """

        self.special_mode_active = False
        """The special mode active state"""

        ##########################
        # Fitness Related Values #
        ##########################

        ######################
        # Evolution tracking #
        ######################
        self.evolution_failure_count = 0
        """The number of times an evolution has failed"""
        self.evolution_success_count = 0
        """The number of times an evolution has succeeded"""

        ########################
        # Bonus stage tracking #
        ########################
        self.diglett_stages_completed = 0
        """The number of Diglett stages completed"""
        self.diglett_stages_visited = 0
        """The number of Diglett stages visited"""
        self.gengar_stages_completed = 0
        """The number of Gengar stages completed"""
        self.gengar_stages_visited = 0
        """The number of Gengar stages visited"""
        self.meowth_stages_completed = 0
        """The number of Meowth stages completed"""
        self.meowth_stages_visited = 0
        """The number of Meowth stages visited"""
        self.mewtwo_stages_completed = 0
        """The number of Mewtwo stages completed"""
        self.mewtwo_stages_visited = 0
        """The number of Mewtwo stages visited"""
        self.seel_stages_completed = 0
        """The number of Seel stages completed"""
        self.seel_stages_visited = 0
        """The number of Seel stages visited"""

        ##########################
        # Pikachu Saver tracking #
        ##########################
        self.pikachu_saver_charge = 0  # range of 0-15
        """The charge of the Pikachu saver, ranges from 0 to 15"""
        self.pikachu_saver_increments = 0
        """The number of times the Pikachu saver charge has incremented"""
        self.pikachu_saver_used = 0
        """The number of times the Pikachu saver has been used"""

        ################
        # Map tracking #
        ################
        self.current_map = 0
        """The current map


        Example:
        ```python
        >>> from pyboy.plugins.game_wrapper_pokemon_pinball import Maps
        >>> pyboy = PyBoy(pokemon_pinball_rom)
        >>> pyboy.game_wrapper.current_map == Maps.PALLET_TOWN.value
        True
        ```
        """
        self.map_change_attempts = 0
        """The number of times a map change has been attempted"""
        self.map_change_successes = 0
        """The number of times a map change has been successful"""

        ###########################
        # Pokemon Caught Tracking #
        ###########################
        self.pokemon_caught_in_session = 0
        """The number of pokemon caught in the current session"""
        self.pokemon_seen_in_session = 0
        """The number of pokemon seen in the current session"""

        #########################
        # Ball upgrade Tracking #
        #########################
        self.great_ball_upgrades = 0
        """The number of Great Ball upgrades obtained"""
        self.ultra_ball_upgrades = 0
        """The number of Ultra Ball upgrades obtained"""
        self.master_ball_upgrades = 0
        """The number of Master Ball upgrades obtained"""

        #######################
        # Extra Ball Tracking #
        #######################
        self.extra_balls_added = 0  # Does not include extra balls rewarded via roulette
        """The number of extra balls added, not including those rewarded via roulette"""

        ##########################
        # Lost Ball During Saver #
        ##########################
        self.lost_ball_during_saver = 0
        """The number of balls lost during a saver mode"""

        #################
        # Slot Tracking #
        #################
        self.roulette_slots_opened = 0
        """The number of roulette slots opened"""
        self.roulette_slots_entered = 0
        """The number of roulette slots entered"""

        super().__init__(*args, game_area_section=(0, 0) + self.shape, game_area_follow_scxy=True, **kwargs)

        if not self.enabled():
            return

        self._add_hooks()

    def _update_pokedex(self):
        for pokemon in Pokemon:
            self.pokedex[pokemon.value] = self.pyboy.memory[ADDR_POKEDEX + pokemon.value]

    def has_pokemon(self, pokemon):
        """
        Check if the player has caught the given pokemon

        Args:
            pokemon (Pokemon): The pokemon to check for
        """
        return self.pokedex[pokemon.value] == 2

    def set_unlimited_saver(self, unlimited_saver=True):
        """
        Sets the unlimited saver mode in the game.

        This function allows for an unlimited saver option in the game.

        Parameters:
        unlimited_saver (bool, optional): If True, the saver mode in the game is unlimited. Defaults to True.

        Returns:
        None
        """
        self._unlimited_saver = unlimited_saver

    def _set_stage(self, stage):
        """
        Override rom memory to set the default stage to the desired stage
        Bonus stages require further initialization
        This method should be called before the game starts

        No ops out these asm lines:
            jr z, .pressedB
            ld a, [wSelectedFieldIndex]
            ld c, a
            ld b, $0
            ld hl, StartingStages
            add hl, bc
            ld a, [hl]

        Inserts the following asm:
            ld a, stage.value


        Kwargs:
            stage (Stage): The stage to set the game to.
        """

        if stage is None:
            return
        for i in range(NO_OP_BYTE_WIDTH_STAGE_OVERRIDE):
            # equivalent to no op
            self.pyboy.memory[ADDR_TO_NO_OP_BANK_STAGE_OVERRIDE, ADDR_TO_NO_OP_STAGE_OVERRIDE + i] = 0x00
        # equivalent to ld a, stage.value
        self.pyboy.memory[ADDR_TO_NO_OP_BANK_STAGE_OVERRIDE, ADDR_TO_NO_OP_STAGE_OVERRIDE] = 0b00111110
        self.pyboy.memory[ADDR_TO_NO_OP_BANK_STAGE_OVERRIDE, ADDR_TO_NO_OP_STAGE_OVERRIDE + 1] = stage.value

    def _init_bonus_stage(self, stage):
        # set backup stage if it is a bonus stage
        if stage in RedBonusStages:
            self.pyboy.memory[ADDR_CURRENT_STAGE_BACKUP] = Stage.RED_BOTTOM.value
        elif stage in BlueBonusStages:
            self.pyboy.memory[ADDR_CURRENT_STAGE_BACKUP] = Stage.BLUE_BOTTOM.value

        # do initializations skipped by loading bonus stage instead of main stage
        if stage in RedBonusStages or stage in BlueBonusStages:
            self.pyboy.memory[ADDR_CURRENT_SLOT_FRAME] = CURRENT_SLOT_FRAME_VALUE
            self.pyboy.memory[ADDR_BALLS_LEFT] = 1
            self.pyboy.memory[ADDR_NUM_BALL_LIVES] = 3
            self.pyboy.memory[ADDR_STAGE_COLLISION_STATE] = 0b100
            self.pyboy.memory[ADDR_STAGE_COLLISION_STATE_HELPER] = 0b100

    def start_catch_mode(self, pokemon=Pokemon.BULBASAUR, unlimited_time=False):
        """
        Starts the catch mode in the game. NOTE: This method does not change stage specific values and may need a top/bottom stage change to work properly.

        This function sets up the game state for catch mode, including the Pokemon to catch and the game timer.

        Parameters:
        pokemon (Pokemon, optional): The Pokemon to catch in this mode. Defaults to Pokemon.BULBASAUR.
        unlimited_time (bool, optional): If True, the game timer is not activated, giving unlimited time in catch mode. Defaults to False.

        Returns:
        None
        """
        # All values are based on PRET disassembly
        self.pyboy.memory[ADDR_SPECIAL_MODE] = SpecialMode.CATCH.value
        self.pyboy.memory[ADDR_POKEMON_TO_CATCH] = pokemon.value
        self.pyboy.memory[ADDR_SPECIAL_MODE_ACTIVE] = 1
        self.pyboy.memory[ADDR_SPECIAL_MODE_STATE] = 0
        self.pyboy.memory[ADDR_D5C6] = 0
        self.pyboy.memory[ADDR_NUM_MON_HITS] = 0
        self.pyboy.memory[ADDR_NUM_CATCH_TILES_FLIPPED] = 0
        self.pyboy.memory[ADDR_TILE_ILLUMINATION : ADDR_TILE_ILLUMINATION + TILE_ILLUMINATION_BYTE_WIDTH] = 0
        if not unlimited_time:
            self.pyboy.memory[ADDR_TIMER_SECONDS] = 0
            self.pyboy.memory[ADDR_TIMER_MINUTES] = 2
            self.pyboy.memory[ADDR_TIMER_FRAMES] = 0
            self.pyboy.memory[ADDR_TIMER_RAN_OUT] = 0
            self.pyboy.memory[ADDR_TIMER_PAUSED] = 0
            self.pyboy.memory[ADDR_TIMER_ACTIVE] = 1
            self.pyboy.memory[ADDR_D580] = 1

    # replaces pause button with evolution start
    def enable_evolve_hack(self, unlimited_time=False):
        """
        Enables the evolution hack in the game.

        This function replaces the pause button with the evolution start method. It also allows for an unlimited time option.

        Parameters:
        unlimited_time (bool, optional): If True, the game timer is disabled, giving unlimited time in the game. Defaults to False.

        Returns:
        None
        """
        bank_addr_evo = BANK_OFFSET_START_EVOLUTION

        lower_8bits = bank_addr_evo[1] & 0xFF
        upper_8bits = (bank_addr_evo[1] >> 8) & 0xFF

        bank_addr_pause = BANK_OFFSET_PAUSE_METHOD_CALL

        self.pyboy.memory[BANK_OFFSET_PAUSE_METHOD_BANK] = bank_addr_evo[0]
        self.pyboy.memory[bank_addr_pause[0], bank_addr_pause[1]] = lower_8bits
        self.pyboy.memory[bank_addr_pause[0], bank_addr_pause[1] + 1] = upper_8bits
        if unlimited_time:

            def disable_timer(context):
                context.memory[ADDR_TIMER_ACTIVE] = 0

            bank = BANK_OFFSET_DISABLE_TIMER[0]
            offset = BANK_OFFSET_DISABLE_TIMER[1]
            try:
                self.pyboy.hook_register(bank, offset, disable_timer, self.pyboy)
            except ValueError:
                pass  # hook already exists

    def current_map_completed(self):
        """
        Determines if all Pokemon in the current map have been caught.

        This function checks whether all Pokemon, both common and rare, in the current stage's map have been caught.
        It supports both Red and Blue stages. If any Pokemon in the map has not been caught, the function returns False.
        If all Pokemon have been caught, it returns True.

        Returns:
        bool: True if all Pokemon in the current map have been caught, False otherwise.
        """
        if self.current_stage in RedStages:
            for pokemon in RedStageMapWildMons[self.current_map]:
                if not self.has_pokemon(pokemon):
                    return False
            for pokemon in RedStageMapWildMonsRare[self.current_map]:
                if not self.has_pokemon(pokemon):
                    return False
        elif self.current_stage in BlueStages:
            for pokemon in BlueStageMapWildMons[self.current_map]:
                if not self.has_pokemon(pokemon):
                    return False
            for pokemon in BlueStageMapWildMonsRare[self.current_map]:
                if not self.has_pokemon(pokemon):
                    return False
        return True

    def start_game(self, timer_div=None, stage=None):
        """
        Starts the game with optional timer division and stage parameters.

        This function sets up the game state, sends the necessary inputs to start the game, and saves the initial game state.

        Parameters:
        timer_div (int, optional): The division value for the game timer. Defaults to None.
        stage (int, optional): The stage to start the game at. Defaults to None.

        Returns:
        None
        """
        if self.game_has_started:
            raise PyBoyException("Gamewrapper already started! Use 'reset' instead.")

        self._set_stage(stage)

        # Random tilemap I observed doesn't change until shortly before input is read
        while self.tilemap_background[10, 10] != 269:
            self.pyboy.tick(1, False, False)

        # tick needed count to get to the point where input is read
        self.pyboy.tick(18, False, False)

        # start game
        self.pyboy.send_input(WindowEvent.PRESS_BUTTON_A)
        self.pyboy.tick(2, False, False)
        self.pyboy.send_input(WindowEvent.RELEASE_BUTTON_A)
        # tick count needed to get to the next point where input is read
        self.pyboy.tick(95, False, False)

        self.pyboy.send_input(WindowEvent.PRESS_BUTTON_A)
        self.pyboy.tick(1, False, False)
        self.pyboy.send_input(WindowEvent.RELEASE_BUTTON_A)
        self.pyboy.tick(1, False, False)

        ticks_until_visible = 74
        self.pyboy.tick(ticks_until_visible, False, False)

        ticks_until_input_ready = 4
        self.pyboy.tick(ticks_until_input_ready, False, False)

        # needs to be called after normal initializations, otherwise it will be overwritten
        self._init_bonus_stage(stage)

        PyBoyGameWrapper.start_game(self, timer_div=timer_div)

    def reset_game(self, timer_div=None):
        """
        After calling `start_game`, use this method to reset the beginning of the game.

        Kwargs:
            timer_div (int): Replace timer's DIV register with this value. Use `None` to randomize.
        """
        PyBoyGameWrapper.reset_game(self, timer_div=timer_div)

    def reset_tracking(self):
        """
        Resets all tracking values to 0.
        """
        self.pokemon_caught_in_session = 0
        self.pokemon_seen_in_session = 0
        self.evolution_failure_count = 0
        self.evolution_success_count = 0
        self.diglett_stages_completed = 0
        self.diglett_stages_visited = 0
        self.gengar_stages_completed = 0
        self.gengar_stages_visited = 0
        self.meowth_stages_completed = 0
        self.meowth_stages_visited = 0
        self.mewtwo_stages_completed = 0
        self.mewtwo_stages_visited = 0
        self.seel_stages_completed = 0
        self.seel_stages_visited = 0
        self.pikachu_saver_increments = 0
        self.pikachu_saver_used = 0
        self.map_change_attempts = 0
        self.map_change_successes = 0
        self.great_ball_upgrades = 0
        self.ultra_ball_upgrades = 0
        self.master_ball_upgrades = 0
        self.extra_balls_added = 0
        self.lost_ball_during_saver = 0
        self.roulette_slots_opened = 0
        self.roulette_slots_entered = 0

    def get_unique_pokemon_caught(self):
        """
        Get the number of unique pokemon caught in the current session based off the in game pokedex
        """
        return self.pokedex.count(2)

    def post_tick(self):
        self._tile_cache_invalid = True
        self._sprite_cache_invalid = True

        self.ball_type = self.pyboy.memory[ADDR_BALL_TYPE]
        self.balls_left = 3 - self.pyboy.memory[ADDR_BALLS_LEFT] + self.pyboy.memory[ADDR_EXTRA_BALLS]
        self.game_over = self.pyboy.memory[ADDR_GAME_OVER] == 1

        self.current_map = self.pyboy.memory[ADDR_CURRENT_MAP]
        self.current_stage = self.pyboy.memory[ADDR_CURRENT_STAGE]

        self.special_mode = self.pyboy.memory[ADDR_SPECIAL_MODE]
        self.special_mode_active = self.pyboy.memory[ADDR_SPECIAL_MODE_ACTIVE] == 1

        self.score = (
            bcd_to_dec(
                int.from_bytes(self.pyboy.memory[ADDR_SCORE : ADDR_SCORE + SCORE_BYTE_WIDTH], "little"),
                byte_width=SCORE_BYTE_WIDTH,
            )
            * 10
        )

        self.multiplier = self.pyboy.memory[ADDR_MULTIPLIER]

        self.ball_size = self.pyboy.memory[ADDR_BALL_SIZE]

        self.ball_x = self.pyboy.memory[ADDR_BALL_X]
        self.ball_y = self.pyboy.memory[ADDR_BALL_Y]
        self.ball_x_velocity = self.pyboy.memory[ADDR_BALL_X_VELOCITY]
        self.ball_y_velocity = self.pyboy.memory[ADDR_BALL_Y_VELOCITY]

        self.pikachu_saver_charge = self.pyboy.memory[ADDR_PIKACHU_SAVER_CHARGE]

        if self._unlimited_saver:
            self.pyboy.memory[ADDR_BALL_SAVER_SECONDS_LEFT] = 30

        self.ball_saver_seconds_left = self.pyboy.memory[ADDR_BALL_SAVER_SECONDS_LEFT]

        self._update_pokedex()

    def __repr__(self):
        # fmt: off
        return (
            "PokemonPinball:\n" +
            "Score: " + str(self.score) + "\n" +
            "Multiplier: " + str(self.multiplier) + "\n" +
            "Balls left: " + str(self.balls_left) + "\n" +
            "Ball type: " + str(BallType(self.ball_type).name) + "\n" +
            "Ball X: " + str(self.ball_x) + "\n" +
            "Ball Y: " + str(self.ball_y) + "\n" +
            "Ball X Velocity: " + str(self.ball_x_velocity) + "\n" +
            "Ball Y Velocity: " + str(self.ball_y_velocity) + "\n" +
            "Current stage: " + str(Stage(self.current_stage).name) + "\n" +
            "Game over: " + str(self.game_over) + "\n" +
            "Ball saver seconds left: " + str(self.ball_saver_seconds_left) + "\n" +
            "Pokemon caught in session: " + str(self.pokemon_caught_in_session) + "\n" +
            "Pokemon seen in session: " + str(self.pokemon_seen_in_session) + "\n" +
            "Ball lost during saver: " + str(self.lost_ball_during_saver) + "\n" +
            "Special mode active: " + str(self.special_mode_active) + "\n" +
            "Evolution failure count: " + str(self.evolution_failure_count) + "\n" +
            "Evolution success count: " + str(self.evolution_success_count) + "\n" +
            "Pikachu saver charge: " + str(self.pikachu_saver_charge) + "\n" +
            "Pikachu saver used: " + str(self.pikachu_saver_used) + "\n" +
            "Great Ball upgrades: " + str(self.great_ball_upgrades) + "\n" +
            "Ultra Ball upgrades: " + str(self.ultra_ball_upgrades) + "\n" +
            "Master Ball upgrades: " + str(self.master_ball_upgrades) + "\n" +
            "Extra balls added: " + str(self.extra_balls_added) + "\n" +
            "Roulette slots opened: " + str(self.roulette_slots_opened) + "\n" +
            "Roulette slots entered: " + str(self.roulette_slots_entered) + "\n" +
            "Current map: " + str(self.current_map) + "\n" +
            "Diglett stages completed: " + str(self.diglett_stages_completed) + " / Visited: " + str(self.diglett_stages_visited) + "\n" +
            "Gengar stages completed: " + str(self.gengar_stages_completed) + " / Visited: " + str(self.gengar_stages_visited) + "\n" +
            "Meowth stages completed: " + str(self.meowth_stages_completed) + " / Visited: " + str(self.meowth_stages_visited) + "\n" +
            "Mewtwo stages completed: " + str(self.mewtwo_stages_completed) + " / Visited: " + str(self.mewtwo_stages_visited) + "\n" +
            "Seel stages completed: " + str(self.seel_stages_completed) + " / Visited: " + str(self.seel_stages_visited) + "\n"
        )
        # fmt: on

    def _add_hooks(self):
        def completed_evolution(context):
            context.evolution_success_count += 1

        self.pyboy.hook_register(
            BANK_OFFSET_COMPLETE_EVOLUTION_MODE_RED_FIELD[0],
            BANK_OFFSET_COMPLETE_EVOLUTION_MODE_RED_FIELD[1],
            completed_evolution,
            self,
        )
        self.pyboy.hook_register(
            BANK_OFFSET_COMPLETE_EVOLUTION_MODE_BLUE_FIELD[0],
            BANK_OFFSET_COMPLETE_EVOLUTION_MODE_BLUE_FIELD[1],
            completed_evolution,
            self,
        )

        def failed_evolution(context):
            context.evolution_failure_count += 1

        self.pyboy.hook_register(
            BANK_OFFSET_FAIL_EVOLUTION_MODE_RED_FIELD[0],
            BANK_OFFSET_FAIL_EVOLUTION_MODE_RED_FIELD[1],
            failed_evolution,
            self,
        )
        self.pyboy.hook_register(
            BANK_OFFSET_FAIL_EVOLUTION_MODE_BLUE_FIELD[0],
            BANK_OFFSET_FAIL_EVOLUTION_MODE_BLUE_FIELD[1],
            failed_evolution,
            self,
        )

        def pokemon_caught(context):
            context.pokemon_caught_in_session += 1

        self.pyboy.hook_register(
            BANK_OFFSET_ADD_CAUGHT_POKEMON_TO_PARTY[0], BANK_OFFSET_ADD_CAUGHT_POKEMON_TO_PARTY[1], pokemon_caught, self
        )

        def pokemon_seen(context):
            context.pokemon_seen_in_session += 1

        self.pyboy.hook_register(
            BANK_OFFSET_SET_POKEMON_SEEN_FLAG[0], BANK_OFFSET_SET_POKEMON_SEEN_FLAG[1], pokemon_seen, self
        )

        def meowth_visited(context):
            context.meowth_stages_visited += 1

        self.pyboy.hook_register(
            BANK_OFFSET_INIT_MEOWTH_BONUS_STAGE[0], BANK_OFFSET_INIT_MEOWTH_BONUS_STAGE[1], meowth_visited, self
        )

        def diglett_visited(context):
            context.diglett_stages_visited += 1

        self.pyboy.hook_register(
            BANK_OFFSET_INIT_DIGLETT_BONUS_STAGE[0], BANK_OFFSET_INIT_DIGLETT_BONUS_STAGE[1], diglett_visited, self
        )

        def gengar_visited(context):
            context.gengar_stages_visited += 1

        self.pyboy.hook_register(
            BANK_OFFSET_INIT_GENGAR_BONUS_STAGE[0], BANK_OFFSET_INIT_GENGAR_BONUS_STAGE[1], gengar_visited, self
        )

        def seel_visited(context):
            context.seel_stages_visited += 1

        self.pyboy.hook_register(
            BANK_OFFSET_INIT_SEEL_BONUS_STAGE[0], BANK_OFFSET_INIT_SEEL_BONUS_STAGE[1], seel_visited, self
        )

        def mewtwo_visited(context):
            context.mewtwo_stages_visited += 1

        self.pyboy.hook_register(
            BANK_OFFSET_INIT_MEWTWO_BONUS_STAGE[0], BANK_OFFSET_INIT_MEWTWO_BONUS_STAGE[1], mewtwo_visited, self
        )

        def meowth_completed(context):
            context.meowth_stages_completed += 1

        self.pyboy.hook_register(
            BANK_OFFSET_MEOWTH_STAGE_COMPLETE[0], BANK_OFFSET_MEOWTH_STAGE_COMPLETE[1], meowth_completed, self
        )

        def diglett_completed(context):
            context.diglett_stages_completed += 1

        self.pyboy.hook_register(
            BANK_OFFSET_DIGLETT_STAGE_COMPLETE[0], BANK_OFFSET_DIGLETT_STAGE_COMPLETE[1], diglett_completed, self
        )

        def gengar_completed(context):
            context.gengar_stages_completed += 1

        self.pyboy.hook_register(
            BANK_OFFSET_GENGAR_STAGE_COMPLETE[0], BANK_OFFSET_GENGAR_STAGE_COMPLETE[1], gengar_completed, self
        )

        def seel_completed(context):
            context.seel_stages_completed += 1

        self.pyboy.hook_register(
            BANK_OFFSET_SEEL_STAGE_COMPLETE[0], BANK_OFFSET_SEEL_STAGE_COMPLETE[1], seel_completed, self
        )

        def mewtwo_completed(context):
            context.mewtwo_stages_completed += 1

        self.pyboy.hook_register(
            BANK_OFFSET_MEWTWO_STAGE_COMPLETE[0], BANK_OFFSET_MEWTWO_STAGE_COMPLETE[1], mewtwo_completed, self
        )

        def map_change_attempt(context):
            context.map_change_attempts += 1

        self.pyboy.hook_register(
            BANK_OFFSET_MAP_CHANGE_ATTEMPT[0], BANK_OFFSET_MAP_CHANGE_ATTEMPT[1], map_change_attempt, self
        )

        def map_change_success(context):
            context.map_change_successes += 1

        self.pyboy.hook_register(
            BANK_OFFSET_MAP_CHANGE_SUCCESS[0], BANK_OFFSET_MAP_CHANGE_SUCCESS[1], map_change_success, self
        )

        def pika_saver_increment(context):
            context.pikachu_saver_increments += 1

        self.pyboy.hook_register(
            BANK_OFFSET_PIKA_SAVER_INCREMENT_BLUE_FIELD[0],
            BANK_OFFSET_PIKA_SAVER_INCREMENT_BLUE_FIELD[1],
            pika_saver_increment,
            self,
        )
        self.pyboy.hook_register(
            BANK_OFFSET_PIKA_SAVER_INCREMENT_RED_FIELD[0],
            BANK_OFFSET_PIKA_SAVER_INCREMENT_RED_FIELD[1],
            pika_saver_increment,
            self,
        )

        def pika_saver_used(context):
            context.pikachu_saver_used += 1

        self.pyboy.hook_register(
            BANK_OFFSET_PIKA_SAVER_USED_BLUE_FIELD[0], BANK_OFFSET_PIKA_SAVER_USED_BLUE_FIELD[1], pika_saver_used, self
        )
        self.pyboy.hook_register(
            BANK_OFFSET_PIKA_SAVER_USED_RED_FIELD[0], BANK_OFFSET_PIKA_SAVER_USED_RED_FIELD[1], pika_saver_used, self
        )

        def ball_upgrade_trigger(context):
            if context.ball_type == BallType.POKEBALL.value:
                context.great_ball_upgrades += 1
            elif context.ball_type == BallType.GREATBALL.value:
                context.ultra_ball_upgrades += 1
            elif context.ball_type == BallType.ULTRABALL.value:
                context.master_ball_upgrades += 1

        self.pyboy.hook_register(
            BANK_OFFSET_BALL_UPGRADE_TRIGGER_BLUE_FIELD[0],
            BANK_OFFSET_BALL_UPGRADE_TRIGGER_BLUE_FIELD[1],
            ball_upgrade_trigger,
            self,
        )
        self.pyboy.hook_register(
            BANK_OFFSET_BALL_UPGRADE_TRIGGER_RED_FIELD[0],
            BANK_OFFSET_BALL_UPGRADE_TRIGGER_RED_FIELD[1],
            ball_upgrade_trigger,
            self,
        )

        def extra_ball_added(context):
            context.extra_balls_added += 1

        self.pyboy.hook_register(BANK_OFFSET_ADD_EXTRA_BALL[0], BANK_OFFSET_ADD_EXTRA_BALL[1], extra_ball_added, self)

        # This prevents slot reward extra ball from being counted as it is mostly RNG based and not a good fitness metric
        def slot_reward_extra_ball(context):
            context.extra_balls_added -= 1

        self.pyboy.hook_register(
            BANK_OFFSET_SLOT_REWARD_EXTRA_BALL[0], BANK_OFFSET_SLOT_REWARD_EXTRA_BALL[1], slot_reward_extra_ball, self
        )

        def opened_slot_by_getting_4_cave_lights(context):
            context.roulette_slots_opened += 1

        self.pyboy.hook_register(
            BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_BLUE[0],
            BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_BLUE[1],
            opened_slot_by_getting_4_cave_lights,
            self,
        )
        self.pyboy.hook_register(
            BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_RED[0],
            BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_RED[1],
            opened_slot_by_getting_4_cave_lights,
            self,
        )

        def slot_reward_roulette(context):
            context.roulette_slots_entered += 1

        self.pyboy.hook_register(
            BANK_OFFSET_SLOT_REWARD_ROULETTE[0], BANK_OFFSET_SLOT_REWARD_ROULETTE[1], slot_reward_roulette, self
        )

        def lost_ball_during_saver(context):
            context.lost_ball_during_saver += 1

        self.pyboy.hook_register(
            BANK_OFFSET_BALL_SAVED_RED[0], BANK_OFFSET_BALL_SAVED_RED[1], lost_ball_during_saver, self
        )
        self.pyboy.hook_register(
            BANK_OFFSET_BALL_SAVED_BLUE[0], BANK_OFFSET_BALL_SAVED_BLUE[1], lost_ball_during_saver, self
        )
