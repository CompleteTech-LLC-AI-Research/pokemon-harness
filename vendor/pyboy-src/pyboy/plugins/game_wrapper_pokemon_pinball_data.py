#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""Pokemon Pinball data tables: Pokedex/Maps enums, RAM addresses and stage maps.

Split out of ``game_wrapper_pokemon_pinball.py`` by the Pokered harness fork to keep
every file below the repository's 1000-line bound (#122/#138). The ``cdef class``
``GameWrapperPokemonPinball`` cannot move: ``pyboy/plugins/manager.pxd`` cimports it as a
Cython type, so it stays in the original module and re-exports everything defined here.
"""

from enum import Enum


class Pokemon(Enum):
    """
    The Pokemon values in the game.
    """

    BULBASAUR = 0
    IVYSAUR = 1
    VENUSAUR = 2
    CHARMANDER = 3
    CHARMELEON = 4
    CHARIZARD = 5
    SQUIRTLE = 6
    WARTORTLE = 7
    BLASTOISE = 8
    CATERPIE = 9
    METAPOD = 10
    BUTTERFREE = 11
    WEEDLE = 12
    KAKUNA = 13
    BEEDRILL = 14
    PIDGEY = 15
    PIDGEOTTO = 16
    PIDGEOT = 17
    RATTATA = 18
    RATICATE = 19
    SPEAROW = 20
    FEAROW = 21
    EKANS = 22
    ARBOK = 23
    PIKACHU = 24
    RAICHU = 25
    SANDSHREW = 26
    SANDSLASH = 27
    NIDORAN_F = 28
    NIDORINA = 29
    NIDOQUEEN = 30
    NIDORAN_M = 31
    NIDORINO = 32
    NIDOKING = 33
    CLEFAIRY = 34
    CLEFABLE = 35
    VULPIX = 36
    NINETALES = 37
    JIGGLYPUFF = 38
    WIGGLYTUFF = 39
    ZUBAT = 40
    GOLBAT = 41
    ODDISH = 42
    GLOOM = 43
    VILEPLUME = 44
    PARAS = 45
    PARASECT = 46
    VENONAT = 47
    VENOMOTH = 48
    DIGLETT = 49
    DUGTRIO = 50
    MEOWTH = 51
    PERSIAN = 52
    PSYDUCK = 53
    GOLDUCK = 54
    MANKEY = 55
    PRIMEAPE = 56
    GROWLITHE = 57
    ARCANINE = 58
    POLIWAG = 59
    POLIWHIRL = 60
    POLIWRATH = 61
    ABRA = 62
    KADABRA = 63
    ALAKAZAM = 64
    MACHOP = 65
    MACHOKE = 66
    MACHAMP = 67
    BELLSPROUT = 68
    WEEPINBELL = 69
    VICTREEBEL = 70
    TENTACOOL = 71
    TENTACRUEL = 72
    GEODUDE = 73
    GRAVELER = 74
    GOLEM = 75
    PONYTA = 76
    RAPIDASH = 77
    SLOWPOKE = 78
    SLOWBRO = 79
    MAGNEMITE = 80
    MAGNETON = 81
    FARFETCH_D = 82
    DODUO = 83
    DODRIO = 84
    SEEL = 85
    DEWGONG = 86
    GRIMER = 87
    MUK = 88
    SHELLDER = 89
    CLOYSTER = 90
    GASTLY = 91
    HAUNTER = 92
    GENGAR = 93
    ONIX = 94
    DROWZEE = 95
    HYPNO = 96
    KRABBY = 97
    KINGLER = 98
    VOLTORB = 99
    ELECTRODE = 100
    EXEGGCUTE = 101
    EXEGGUTOR = 102
    CUBONE = 103
    MAROWAK = 104
    HITMONLEE = 105
    HITMONCHAN = 106
    LICKITUNG = 107
    KOFFING = 108
    WEEZING = 109
    RHYHORN = 110
    RHYDON = 111
    CHANSEY = 112
    TANGELA = 113
    KANGASKHAN = 114
    HORSEA = 115
    SEADRA = 116
    GOLDEEN = 117
    SEAKING = 118
    STARYU = 119
    STARMIE = 120
    MR_MIME = 121
    SCYTHER = 122
    JYNX = 123
    ELECTABUZZ = 124
    MAGMAR = 125
    PINSIR = 126
    TAUROS = 127
    MAGIKARP = 128
    GYARADOS = 129
    LAPRAS = 130
    DITTO = 131
    EEVEE = 132
    VAPOREON = 133
    JOLTEON = 134
    FLAREON = 135
    PORYGON = 136
    OMANYTE = 137
    OMASTAR = 138
    KABUTO = 139
    KABUTOPS = 140
    AERODACTYL = 141
    SNORLAX = 142
    ARTICUNO = 143
    ZAPDOS = 144
    MOLTRES = 145
    DRATINI = 146
    DRAGONAIR = 147
    DRAGONITE = 148
    MEWTWO = 149
    MEW = 150


class Maps(Enum):
    """
    The map values in the game.
    """

    PALLET_TOWN = 0
    VIRIDIAN_CITY = 1
    VIRIDIAN_FOREST = 2
    PEWTER_CITY = 3
    MT_MOON = 4
    CERULEAN_CITY = 5
    VERMILION_SEASIDE = 6
    VERMILION_STREETS = 7
    ROCK_MOUNTAIN = 8
    LAVENDER_TOWN = 9
    CELADON_CITY = 10
    CYCLING_ROAD = 11
    FUCHSIA_CITY = 12
    SAFARI_ZONE = 13
    SAFFRON_CITY = 14
    SEAFOAM_ISLANDS = 15
    CINNABAR_ISLAND = 16
    INDIGO_PLATEAU = 17


#################
# RAM Addresses #
#################

# value starts at 1, increments by 1 for each new ball launch and compares to ADDR_NUM_BALL_LIVES
ADDR_BALLS_LEFT = 0xD49D

# value gets initialized to 3 by red and blue stage initialization code
ADDR_NUM_BALL_LIVES = 0xD49E

ADDR_BALL_TYPE = 0xD47E
ADDR_BALL_SIZE = 0xD4C8
ADDR_EXTRA_BALLS = 0xD49B

ADDR_BALL_X = 0xD4B3
ADDR_BALL_Y = 0xD4B5
ADDR_BALL_X_VELOCITY = 0xD4BB
ADDR_BALL_Y_VELOCITY = 0xD4BD

ADDR_BALL_SAVER_SECONDS_LEFT = 0xD4A4

ADDR_NUM_MON_HITS = 0xD5C0
ADDR_NUM_CATCH_TILES_FLIPPED = 0xD5B6
ADDR_TILE_ILLUMINATION = 0xD586
TILE_ILLUMINATION_BYTE_WIDTH = 48

ADDR_MESSAGE_BUFFER = 0xC600
MESSAGE_BUFFER_BYTE_WIDTH = 100

ADDR_WHICH_DIGLETT = 0xD4ED
ADDR_RIGHT_MAP_MOVE_COUNTER = 0xD4F2
ADDR_LEFT_MAP_MOVE_COUNTER = 0xD4F0

ADDR_TIMER_SECONDS = 0xD57A
ADDR_TIMER_MINUTES = 0xD57B
ADDR_TIMER_FRAMES = 0xD57C
ADDR_TIMER_RAN_OUT = 0xD57E  # 1 = ran out
ADDR_TIMER_PAUSED = 0xD57F  # nz = paused
ADDR_TIMER_ACTIVE = 0xD57D  # 1 = active
ADDR_D580 = 0xD580  # Something to do with the timer, needs to be initialized.

ADDR_CURRENT_MAP = 0xD54A

ADDR_D5C6 = 0xD5C6  # Something to do with the catch mode, needs to be initialized.

ADDR_POKEDEX = 0xD962

ADDR_STAGE_COLLISION_STATE = 0xD4AF
ADDR_STAGE_COLLISION_STATE_HELPER = 0xD7AD

ADDR_CURRENT_STAGE = 0xD4AC
ADDR_CURRENT_STAGE_BACKUP = 0xD4AD

ADDR_BONUS_STAGE_WON = 0xD49A

ADDR_PIKACHU_SAVER_CHARGE = 0xD517
PIKACHU_SAVER_CHARGE_MAX = 15

ADDR_CURRENT_SLOT_FRAME = 0xD603
# this is the value needed to properly return to main stage after bonus stage
# it sets the current slot frame to 7, which is the frame before getting spit out after bonus stage
CURRENT_SLOT_FRAME_VALUE = 0x7

ADDR_SCORE = 0xD46A
SCORE_BYTE_WIDTH = 6
MAX_SCORE = 999999999999 * 10
"""The maximum score possible, multiplied by ten to add the implied extra 0 the game uses"""

ADDR_GAME_OVER = 0xD616
ADDR_MULTIPLIER = 0xD482

ADDR_POKEMON_TO_CATCH = 0xD579
ADDR_RARE_POKEMON_FLAG = 0xD55B

ADDR_SPECIAL_MODE = 0xD550
ADDR_SPECIAL_MODE_ACTIVE = 0xD54B
ADDR_SPECIAL_MODE_STATE = 0xD54D  # 0 = handleEvolutionMode, 1 = CompleteEvolutionMode, 2 = FailEvolutionMode
# see here: https://github.com/pret/pokepinball/blob/dcfffa520017ba89108f8be97f51d76c68ea44c9/engine/pinball_game/evolution_mode/evolution_mode_blue_field.asm#L34

#######################
# ROM Bank and Offset #
#######################

ADDR_TO_NO_OP_STAGE_OVERRIDE = 0x1774 + 37
ADDR_TO_NO_OP_BANK_STAGE_OVERRIDE = 3
NO_OP_BYTE_WIDTH_STAGE_OVERRIDE = 11

# Evolution hack related addresses
BANK_OFFSET_START_EVOLUTION = (4, 0x4AB3)
BANK_OFFSET_PAUSE_METHOD_BANK = (3, 0x5954)
BANK_OFFSET_PAUSE_METHOD_CALL = (3, 0x5956)

BANK_OFFSET_COMPLETE_EVOLUTION_MODE_BLUE_FIELD = (8, 0x4D30)
BANK_OFFSET_COMPLETE_EVOLUTION_MODE_RED_FIELD = (8, 0x470B)
BANK_OFFSET_FAIL_EVOLUTION_MODE_BLUE_FIELD = (8, 0x4D7C)
BANK_OFFSET_FAIL_EVOLUTION_MODE_RED_FIELD = (8, 0x4757)
BANK_OFFSET_ADD_CAUGHT_POKEMON_TO_PARTY = (4, 0x473D)
BANK_OFFSET_SET_POKEMON_SEEN_FLAG = (4, 0x4753)
BANK_OFFSET_INIT_DIGLETT_BONUS_STAGE = (6, 0x59F2)
BANK_OFFSET_INIT_MEOWTH_BONUS_STAGE = (9, 0x4000)
BANK_OFFSET_INIT_GENGAR_BONUS_STAGE = (6, 0x4099)
BANK_OFFSET_INIT_SEEL_BONUS_STAGE = (9, 0x5A7C)
BANK_OFFSET_INIT_MEWTWO_BONUS_STAGE = (6, 0x524F)
BANK_OFFSET_DIGLETT_STAGE_COMPLETE = (6, 0x6BF2)
BANK_OFFSET_GENGAR_STAGE_COMPLETE = (6, 0x4A14)
BANK_OFFSET_MEOWTH_STAGE_COMPLETE = (9, 0x444B)
BANK_OFFSET_SEEL_STAGE_COMPLETE = (9, 0x5C5A)
BANK_OFFSET_MEWTWO_STAGE_COMPLETE = (6, 0x565E)
BANK_OFFSET_MAP_CHANGE_ATTEMPT = (0xC, 0x41EC)
BANK_OFFSET_MAP_CHANGE_SUCCESS = (0xC, 0x55D5)
BANK_OFFSET_PIKA_SAVER_INCREMENT_BLUE_FIELD = (0x7, 0x4AFF)
BANK_OFFSET_PIKA_SAVER_INCREMENT_RED_FIELD = (0x5, 0x4E8A)
BANK_OFFSET_PIKA_SAVER_USED_BLUE_FIELD = (0x7, 0x50C9)
BANK_OFFSET_PIKA_SAVER_USED_RED_FIELD = (0x5, 0x6634)
BANK_OFFSET_BALL_UPGRADE_TRIGGER_BLUE_FIELD = (0x7, 0x63DE)
BANK_OFFSET_BALL_UPGRADE_TRIGGER_RED_FIELD = (0x5, 0x53C0)
BANK_OFFSET_ADD_EXTRA_BALL = (0xC, 0x4164)
BANK_OFFSET_SLOT_REWARD_EXTRA_BALL = (0x3, 0x6FA7)
BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_BLUE = (0x7, 0x667E)
BANK_OFFSET_OPENED_SLOT_BY_GETTING_4_CAVE_LIGHTS_RED = (0x5, 0x5284)
BANK_OFFSET_SLOT_REWARD_ROULETTE = (0x3, 0x6D8E)
BANK_OFFSET_DISABLE_TIMER = (4, 0x4D64)
BANK_OFFSET_BALL_SAVED_RED = (3, 0x5D7F)
BANK_OFFSET_BALL_SAVED_BLUE = (3, 0x5E58)

"""The wild Pokemon that can be found in each map in the Red stage, along with their encounter rates"""
RedStageMapWildMons = {
    Maps.PALLET_TOWN: {
        Pokemon.BULBASAUR: 0.0625,
        Pokemon.CHARMANDER: 0.375,
        Pokemon.PIDGEY: 0.1875,
        Pokemon.RATTATA: 0.1875,
        Pokemon.NIDORAN_M: 0.0625,
        Pokemon.POLIWAG: 0.0625,
        Pokemon.TENTACOOL: 0.0625,
    },
    Maps.VIRIDIAN_FOREST: {
        Pokemon.WEEDLE: 0.3125,
        Pokemon.PIDGEY: 0.3125,
        Pokemon.RATTATA: 0.3125,
        Pokemon.PIKACHU: 0.0625,
    },
    Maps.PEWTER_CITY: {
        Pokemon.PIDGEY: 0.125,
        Pokemon.SPEAROW: 0.375,
        Pokemon.EKANS: 0.0625,
        Pokemon.JIGGLYPUFF: 0.3125,
        Pokemon.MAGIKARP: 0.125,
    },
    Maps.CERULEAN_CITY: {
        Pokemon.WEEDLE: 0.125,
        Pokemon.PIDGEY: 0.0625,
        Pokemon.ODDISH: 0.3125,
        Pokemon.PSYDUCK: 0.0625,
        Pokemon.MANKEY: 0.1875,
        Pokemon.ABRA: 0.125,
        Pokemon.KRABBY: 0.0625,
        Pokemon.GOLDEEN: 0.0625,
    },
    Maps.VERMILION_SEASIDE: {
        Pokemon.PIDGEY: 0.0625,
        Pokemon.SPEAROW: 0.0625,
        Pokemon.EKANS: 0.125,
        Pokemon.ODDISH: 0.125,
        Pokemon.MANKEY: 0.125,
        Pokemon.SHELLDER: 0.1875,
        Pokemon.DROWZEE: 0.125,
        Pokemon.KRABBY: 0.1875,
    },
    Maps.ROCK_MOUNTAIN: {
        Pokemon.RATTATA: 0.0625,
        Pokemon.SPEAROW: 0.0625,
        Pokemon.EKANS: 0.1875,
        Pokemon.ZUBAT: 0.0625,
        Pokemon.DIGLETT: 0.1875,
        Pokemon.MACHOP: 0.0625,
        Pokemon.GEODUDE: 0.0625,
        Pokemon.SLOWPOKE: 0.0625,
        Pokemon.ONIX: 0.0625,
        Pokemon.VOLTORB: 0.1875,
    },
    Maps.LAVENDER_TOWN: {
        Pokemon.PIDGEY: 0.125,
        Pokemon.EKANS: 0.125,
        Pokemon.MANKEY: 0.125,
        Pokemon.GROWLITHE: 0.125,
        Pokemon.MAGNEMITE: 0.125,
        Pokemon.GASTLY: 0.3125,
        Pokemon.CUBONE: 0.0625,
    },
    Maps.CYCLING_ROAD: {
        Pokemon.RATTATA: 0.125,
        Pokemon.SPEAROW: 0.125,
        Pokemon.TENTACOOL: 0.125,
        Pokemon.DODUO: 0.1875,
        Pokemon.KRABBY: 0.125,
        Pokemon.LICKITUNG: 0.0625,
        Pokemon.GOLDEEN: 0.125,
        Pokemon.MAGIKARP: 0.125,
    },
    Maps.SAFARI_ZONE: {
        Pokemon.NIDORAN_M: 0.25,
        Pokemon.PARAS: 0.25, 
        Pokemon.DODUO: 0.25,
        Pokemon.RHYHORN: 0.25
    },
    Maps.SEAFOAM_ISLANDS: {
        Pokemon.ZUBAT: 0.0625,
        Pokemon.PSYDUCK: 0.0625,
        Pokemon.TENTACOOL: 0.0625,
        Pokemon.SLOWPOKE: 0.0625,
        Pokemon.SEEL: 0.0625,
        Pokemon.SHELLDER: 0.0625,
        Pokemon.KRABBY: 0.0625,
        Pokemon.HORSEA: 0.25,
        Pokemon.GOLDEEN: 0.0625,
        Pokemon.STARYU: 0.25,
    },
    Maps.CINNABAR_ISLAND: {
        Pokemon.GROWLITHE: 0.25,
        Pokemon.PONYTA: 0.25,
        Pokemon.GRIMER: 0.125,
        Pokemon.KOFFING: 0.25,
        Pokemon.TANGELA: 0.125,
    },
    Maps.INDIGO_PLATEAU: {
        Pokemon.SPEAROW: 0.0625,
        Pokemon.EKANS: 0.0625,
        Pokemon.ZUBAT: 0.125,
        Pokemon.MACHOP: 0.1875,
        Pokemon.GEODUDE: 0.1875,
        Pokemon.ONIX: 0.1875,
        Pokemon.DITTO: 0.1875,
    },
}


RedStageMapWildMonsRare = {
    Maps.PALLET_TOWN: {
        Pokemon.BULBASAUR: 0.1875,
        Pokemon.CHARMANDER: 0.0625,
        Pokemon.PIDGEY: 0.0625,
        Pokemon.RATTATA: 0.0625,
        Pokemon.NIDORAN_M: 0.1875,
        Pokemon.POLIWAG: 0.25,
        Pokemon.TENTACOOL: 0.1875,
    },
    Maps.VIRIDIAN_FOREST: {
        Pokemon.CATERPIE: 0.125,
        Pokemon.WEEDLE: 0.1875,
        Pokemon.PIDGEY: 0.125,
        Pokemon.RATTATA: 0.125,
        Pokemon.PIKACHU: 0.4375,
    },
    Maps.PEWTER_CITY: {
        Pokemon.PIDGEY: 0.125,
        Pokemon.SPEAROW: 0.1875,
        Pokemon.EKANS: 0.25,
        Pokemon.JIGGLYPUFF: 0.1875,
        Pokemon.MAGIKARP: 0.25,
    },
    Maps.CERULEAN_CITY: {
        Pokemon.CATERPIE: 0.0625,
        Pokemon.NIDORAN_M: 0.0625,
        Pokemon.ODDISH: 0.0625,
        Pokemon.PSYDUCK: 0.125,
        Pokemon.MANKEY: 0.125,
        Pokemon.ABRA: 0.1875,
        Pokemon.KRABBY: 0.0625,
        Pokemon.GOLDEEN: 0.125,
        Pokemon.JYNX: 0.1875,
    },
    Maps.VERMILION_SEASIDE: {
        Pokemon.EKANS: 0.25,
        Pokemon.ODDISH: 0.0625,
        Pokemon.MANKEY: 0.0625,
        Pokemon.FARFETCH_D: 0.25,
        Pokemon.SHELLDER: 0.125,
        Pokemon.DROWZEE: 0.125,
        Pokemon.KRABBY: 0.125,
    },
    Maps.ROCK_MOUNTAIN: {
        Pokemon.ZUBAT: 0.125,
        Pokemon.DIGLETT: 0.0625,
        Pokemon.MACHOP: 0.125,
        Pokemon.GEODUDE: 0.125,
        Pokemon.SLOWPOKE: 0.125,
        Pokemon.ONIX: 0.125,
        Pokemon.VOLTORB: 0.125,
        Pokemon.MR_MIME: 0.1875,
    },
    Maps.LAVENDER_TOWN: {
        Pokemon.EKANS: 0.0625,
        Pokemon.MANKEY: 0.0625,
        Pokemon.GROWLITHE: 0.0625,
        Pokemon.MAGNEMITE: 0.125,
        Pokemon.GASTLY: 0.125,
        Pokemon.CUBONE: 0.1875,
        Pokemon.ELECTABUZZ: 0.1875,
        Pokemon.ZAPDOS: 0.1875,
    },
    Maps.CYCLING_ROAD: {
        Pokemon.TENTACOOL: 0.0625,
        Pokemon.DODUO: 0.3125,
        Pokemon.KRABBY: 0.0625,
        Pokemon.LICKITUNG: 0.25,
        Pokemon.GOLDEEN: 0.0625,
        Pokemon.MAGIKARP: 0.0625,
        Pokemon.SNORLAX: 0.1875,
    },
    Maps.SAFARI_ZONE: {
        Pokemon.NIDORAN_M: 0.125,
        Pokemon.PARAS: 0.125,
        Pokemon.RHYHORN: 0.125,
        Pokemon.CHANSEY: 0.25,
        Pokemon.SCYTHER: 0.125,
        Pokemon.TAUROS: 0.125,
        Pokemon.DRATINI: 0.125,
    },
    Maps.SEAFOAM_ISLANDS: {
        Pokemon.SEEL: 0.3125,
        Pokemon.GOLDEEN: 0.25,
        Pokemon.STARYU: 0.25,
        Pokemon.ARTICUNO: 0.1875
    },
    Maps.CINNABAR_ISLAND: {
        Pokemon.GROWLITHE: 0.125,
        Pokemon.PONYTA: 0.125,
        Pokemon.GRIMER: 0.0625,
        Pokemon.KOFFING: 0.125,
        Pokemon.TANGELA: 0.1875,
        Pokemon.OMANYTE: 0.1875,
        Pokemon.KABUTO: 0.1875,
    },
    Maps.INDIGO_PLATEAU: {
        Pokemon.SPEAROW: 0.0625,
        Pokemon.EKANS: 0.0625,
        Pokemon.ZUBAT: 0.0625,
        Pokemon.MACHOP: 0.0625,
        Pokemon.GEODUDE: 0.0625,
        Pokemon.ONIX: 0.0625,
        Pokemon.DITTO: 0.25,
        Pokemon.MOLTRES: 0.1875,
        Pokemon.MEWTWO: 0.1875,
        Pokemon.MEW: 0.0625,
    },
}
"""The wild Pokemon that can be found in each map in the Blue stage, along with their encounter rates"""

BlueStageMapWildMons = {
    Maps.VIRIDIAN_CITY: {
        Pokemon.BULBASAUR: 0.0625,
        Pokemon.SQUIRTLE: 0.3125,
        Pokemon.SPEAROW: 0.0625,
        Pokemon.NIDORAN_F: 0.1875,
        Pokemon.NIDORAN_M: 0.1875,
        Pokemon.POLIWAG: 0.0625,
        Pokemon.TENTACOOL: 0.0625,
        Pokemon.GOLDEEN: 0.0625,
    },
    Maps.VIRIDIAN_FOREST: {
        Pokemon.CATERPIE: 0.3125,
        Pokemon.PIDGEY: 0.3125,
        Pokemon.RATTATA: 0.3125,
        Pokemon.PIKACHU: 0.0625,
    },
    Maps.MT_MOON: {
        Pokemon.RATTATA: 0.0625,
        Pokemon.SPEAROW: 0.125,
        Pokemon.EKANS: 0.125,
        Pokemon.SANDSHREW: 0.125,
        Pokemon.ZUBAT: 0.125,
        Pokemon.PARAS: 0.125,
        Pokemon.PSYDUCK: 0.0625,
        Pokemon.GEODUDE: 0.125,
        Pokemon.KRABBY: 0.0625,
        Pokemon.GOLDEEN: 0.0625,
    },
    Maps.CERULEAN_CITY: {
        Pokemon.CATERPIE: 0.125,
        Pokemon.PIDGEY: 0.0625,
        Pokemon.MEOWTH: 0.1875,
        Pokemon.PSYDUCK: 0.0625,
        Pokemon.ABRA: 0.125,
        Pokemon.BELLSPROUT: 0.3125,
        Pokemon.KRABBY: 0.0625,
        Pokemon.GOLDEEN: 0.0625,
    },
    Maps.VERMILION_STREETS: {
        Pokemon.PIDGEY: 0.0625,
        Pokemon.SPEAROW: 0.0625,
        Pokemon.SANDSHREW: 0.125,
        Pokemon.MEOWTH: 0.125,
        Pokemon.BELLSPROUT: 0.125,
        Pokemon.SHELLDER: 0.1875,
        Pokemon.DROWZEE: 0.125,
        Pokemon.KRABBY: 0.1875,
    },
    Maps.ROCK_MOUNTAIN: {
        Pokemon.RATTATA: 0.0625,
        Pokemon.SPEAROW: 0.0625,
        Pokemon.SANDSHREW: 0.125,
        Pokemon.ZUBAT: 0.0625,
        Pokemon.DIGLETT: 0.25,
        Pokemon.MACHOP: 0.0625,
        Pokemon.GEODUDE: 0.0625,
        Pokemon.SLOWPOKE: 0.0625,
        Pokemon.ONIX: 0.0625,
        Pokemon.VOLTORB: 0.1875,
    },
    Maps.CELADON_CITY: {
        Pokemon.PIDGEY: 0.125,
        Pokemon.VULPIX: 0.125,
        Pokemon.ODDISH: 0.125,
        Pokemon.MEOWTH: 0.1875,
        Pokemon.MANKEY: 0.1875,
        Pokemon.GROWLITHE: 0.125,
        Pokemon.BELLSPROUT: 0.125,
    },
    Maps.FUCHSIA_CITY: {
        Pokemon.VENONAT: 0.125,
        Pokemon.KRABBY: 0.1875,
        Pokemon.EXEGGCUTE: 0.125,
        Pokemon.KANGASKHAN: 0.125,
        Pokemon.GOLDEEN: 0.1875,
        Pokemon.MAGIKARP: 0.25,
    },
    Maps.SAFARI_ZONE: {
        Pokemon.NIDORAN_F: 0.25,
        Pokemon.PARAS: 0.25,
        Pokemon.DODUO: 0.25,
        Pokemon.RHYHORN: 0.25
    },
    Maps.SAFFRON_CITY: {
        Pokemon.PIDGEY: 0.125,
        Pokemon.EKANS: 0.1875,
        Pokemon.SANDSHREW: 0.1875,
        Pokemon.VULPIX: 0.0625,
        Pokemon.ODDISH: 0.125,
        Pokemon.MEOWTH: 0.0625,
        Pokemon.MANKEY: 0.0625,
        Pokemon.GROWLITHE: 0.0625,
        Pokemon.BELLSPROUT: 0.125,
    },
    Maps.CINNABAR_ISLAND: {
        Pokemon.VULPIX: 0.1875,
        Pokemon.PONYTA: 0.3125,
        Pokemon.GRIMER: 0.125,
        Pokemon.KOFFING: 0.25,
        Pokemon.TANGELA: 0.125,
    },
    Maps.INDIGO_PLATEAU: {
        Pokemon.SPEAROW: 0.0625,
        Pokemon.SANDSHREW: 0.0625,
        Pokemon.ZUBAT: 0.125,
        Pokemon.MACHOP: 0.1875,
        Pokemon.GEODUDE: 0.1875,
        Pokemon.ONIX: 0.1875,
        Pokemon.DITTO: 0.1875,
    },
}

BlueStageMapWildMonsRare = {
    Maps.VIRIDIAN_CITY: {
        Pokemon.BULBASAUR: 0.1875,
        Pokemon.SQUIRTLE: 0.0625,
        Pokemon.SPEAROW: 0.125,
        Pokemon.NIDORAN_F: 0.125,
        Pokemon.NIDORAN_M: 0.125,
        Pokemon.POLIWAG: 0.125,
        Pokemon.TENTACOOL: 0.125,
        Pokemon.GOLDEEN: 0.125,
    },
    Maps.VIRIDIAN_FOREST: {
        Pokemon.CATERPIE: 0.1875,
        Pokemon.WEEDLE: 0.125,
        Pokemon.PIDGEY: 0.125,
        Pokemon.RATTATA: 0.125,
        Pokemon.PIKACHU: 0.4375,
    },
    Maps.MT_MOON: {
        Pokemon.EKANS: 0.125,
        Pokemon.SANDSHREW: 0.125,
        Pokemon.CLEFAIRY: 0.375,
        Pokemon.ZUBAT: 0.125,
        Pokemon.PARAS: 0.125,
        Pokemon.GEODUDE: 0.125,
    },
    Maps.CERULEAN_CITY: {
        Pokemon.WEEDLE: 0.0625,
        Pokemon.NIDORAN_M: 0.0625,
        Pokemon.MEOWTH: 0.125,
        Pokemon.PSYDUCK: 0.125,
        Pokemon.ABRA: 0.1875,
        Pokemon.BELLSPROUT: 0.0625,
        Pokemon.KRABBY: 0.0625,
        Pokemon.GOLDEEN: 0.125,
        Pokemon.JYNX: 0.1875,
    },
    Maps.VERMILION_STREETS: {
        Pokemon.SANDSHREW: 0.25,
        Pokemon.MEOWTH: 0.0625,
        Pokemon.BELLSPROUT: 0.0625,
        Pokemon.FARFETCH_D: 0.25,
        Pokemon.SHELLDER: 0.125,
        Pokemon.DROWZEE: 0.125,
        Pokemon.KRABBY: 0.125,
    },
    Maps.ROCK_MOUNTAIN: {
        Pokemon.ZUBAT: 0.125,
        Pokemon.DIGLETT: 0.0625,
        Pokemon.MACHOP: 0.125,
        Pokemon.GEODUDE: 0.125,
        Pokemon.SLOWPOKE: 0.125,
        Pokemon.ONIX: 0.125,
        Pokemon.VOLTORB: 0.125,
        Pokemon.MR_MIME: 0.1875,
    },
    Maps.CELADON_CITY: {
        Pokemon.CLEFAIRY: 0.125,
        Pokemon.ABRA: 0.125,
        Pokemon.SCYTHER: 0.0625,
        Pokemon.PINSIR: 0.0625,
        Pokemon.EEVEE: 0.1875,
        Pokemon.PORYGON: 0.25,
        Pokemon.DRATINI: 0.1875,
    },
    Maps.FUCHSIA_CITY: {
        Pokemon.VENONAT: 0.25,
        Pokemon.KRABBY: 0.0625,
        Pokemon.EXEGGCUTE: 0.25,
        Pokemon.KANGASKHAN: 0.25,
        Pokemon.GOLDEEN: 0.0625,
        Pokemon.MAGIKARP: 0.125,
    },
    Maps.SAFARI_ZONE: {
        Pokemon.NIDORAN_F: 0.125,
        Pokemon.PARAS: 0.125,
        Pokemon.RHYHORN: 0.125,
        Pokemon.CHANSEY: 0.25,
        Pokemon.PINSIR: 0.125,
        Pokemon.TAUROS: 0.125,
        Pokemon.DRATINI: 0.125,
    },
    Maps.SAFFRON_CITY: {
        Pokemon.PIDGEY: 0.0625,
        Pokemon.EKANS: 0.0625,
        Pokemon.SANDSHREW: 0.0625,
        Pokemon.VULPIX: 0.0625,
        Pokemon.MEOWTH: 0.0625,
        Pokemon.MANKEY: 0.0625,
        Pokemon.GROWLITHE: 0.0625,
        Pokemon.HITMONLEE: 0.1875,
        Pokemon.HITMONCHAN: 0.1875,
        Pokemon.LAPRAS: 0.1875,
    },
    Maps.CINNABAR_ISLAND: {
        Pokemon.VULPIX: 0.0625,
        Pokemon.PONYTA: 0.125,
        Pokemon.GRIMER: 0.125,
        Pokemon.KOFFING: 0.125,
        Pokemon.TANGELA: 0.1875,
        Pokemon.MAGMAR: 0.1875,
        Pokemon.AERODACTYL: 0.1875,
    },
    Maps.INDIGO_PLATEAU: {
        Pokemon.SPEAROW: 0.0625,
        Pokemon.SANDSHREW: 0.0625,
        Pokemon.ZUBAT: 0.0625,
        Pokemon.MACHOP: 0.0625,
        Pokemon.GEODUDE: 0.0625,
        Pokemon.ONIX: 0.0625,
        Pokemon.DITTO: 0.25,
        Pokemon.MOLTRES: 0.1875,
        Pokemon.MEWTWO: 0.1875,
        Pokemon.MEW: 0.0625,
    },
}
