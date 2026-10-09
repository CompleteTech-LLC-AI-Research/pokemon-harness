"""Private fixtures and engines for the observer contract tests (not collected).

Holds the fake sessions, the frozen continuation-pin literals, the canonical N3/N4 peer scripts,
the real-observer flow engine and the in-memory counterfactual mutants so the public test IDs
stay in ``tests/test_battle_observer_contract.py``.
"""

from __future__ import annotations

from tests import _battle_turn_evidence as evidence
from tests.test_battle_turn_evidence import source_mutant, verify_battle_turns

# Frozen from the reviewed plan (sym_anchors_and_signatures): version hook anchor-label anchor-address
# hook-address signature.  Deliberately NOT derived from production _CONTINUATION_PINS.
FROZEN_PINS = """
red player_action_ko_return MirrorMoveCheck.notDone 57b9 57c8 21e6cf2a46b0c8cdb662
red enemy_action_ko_return EnemyCheckIfMirrorMoveEffect.handleExplosionMiss 683e 684d 2115d02a46b0c8cdb662
red terminal_victory TrainerBattleVictory 4696 4699 06fcfa5cd0a72002
red terminal_blackout HandlePlayerBlackOut 4837 4837 fa2bd1fe042824fa59d0
blue player_action_ko_return MirrorMoveCheck.notDone 57b9 57c8 21e6cf2a46b0c8cdb662
blue enemy_action_ko_return EnemyCheckIfMirrorMoveEffect.handleExplosionMiss 683e 684d 2115d02a46b0c8cdb662
blue terminal_victory TrainerBattleVictory 4696 4699 06fcfa5cd0a72002
blue terminal_blackout HandlePlayerBlackOut 4837 4837 fa2bd1fe042824fa59d0
yellow player_action_ko_return MirrorMoveCheck.notDone 592b 593a 21e5cf2a46b0c8cd2864
yellow enemy_action_ko_return EnemyCheckIfMirrorMoveEffect.handleExplosionMiss 69c4 69d3 2114d02a46b0c8cd2864
yellow terminal_victory TrainerBattleVictory 46b8 46bb 06fcfa5bd0a72002
yellow terminal_blackout HandlePlayerBlackOut 489c 489c fa2ad1fe042824fa58d0
"""
# Seven complete continuation hooks per version (bank 0x0f), written out as literals.
FROZEN_LOCATIONS_TEXT = """
red post_exchange=5574 local_fully_paralyzed=594c enemy_fully_paralyzed=69cd player_action_ko_return=57c8 enemy_action_ko_return=684d terminal_victory=4699 terminal_blackout=4837
blue post_exchange=5574 local_fully_paralyzed=594c enemy_fully_paralyzed=69cd player_action_ko_return=57c8 enemy_action_ko_return=684d terminal_victory=4699 terminal_blackout=4837
yellow post_exchange=56e6 local_fully_paralyzed=5abe enemy_fully_paralyzed=6b53 player_action_ko_return=593a enemy_action_ko_return=69d3 terminal_victory=46bb terminal_blackout=489c
"""


def _frozen_tables():
    pins, locations = {}, {}
    for line in FROZEN_PINS.strip().splitlines():
        version, hook, label, anchor, address, signature = line.split()
        pins[version, hook] = (label, int(anchor, 16), int(address, 16), signature)
    for line in FROZEN_LOCATIONS_TEXT.strip().splitlines():
        version, *items = line.split()
        locations[version] = [
            (name, 15, int(address, 16)) for name, address in (i.split("=") for i in items)
        ]
    return pins, locations


FROZEN_PINS_BY, FROZEN_LOCATIONS = _frozen_tables()


_PROVIDER = {
    "red": {
        "select": 0x5564,
        "exchange": 0x5605,
        "load": 0x3725,
        "text": 0x5A4C,
        "printer": 0x3C49,
        "player": 0x5952,
        "enemy": 0x69D3,
    },
    "yellow": {
        "select": 0x56D6,
        "exchange": 0x5777,
        "load": 0x371B,
        "text": 0x5BBE,
        "printer": 0x3C36,
        "player": 0x5AC4,
        "enemy": 0x6B59,
    },
}


_HOOK_SHORT = {
    "player_ko": "player_action_ko_return",
    "enemy_ko": "enemy_action_ko_return",
    "victory": "terminal_victory",
    "blackout": "terminal_blackout",
}


_BYTE_CONTROL = {  # hook -> (index, replacement byte)
    "player_action_ko_return": (6, 0xC0),
    "enemy_action_ko_return": (6, 0xC0),
    "terminal_victory": (4, 0xD1),
    "terminal_blackout": (9, 0xD1),
}


class _Mem(dict):
    def __missing__(self, key):
        return 0


class _FakePyboy:
    def __init__(self, memory):
        self.memory, self.registered, self.callbacks = memory, [], {}

    def hook_register(self, bank, address, callback, context):
        self.registered.append((bank, address))
        self.callbacks.setdefault((bank, address), []).append(callback)


class _FakeSymbols:
    def __init__(self, table):
        self.table = table

    def bank_addr(self, name):
        return self.table[name]

    def __contains__(self, name):
        return name in self.table

    def addr_of(self, name):
        return self.table[name][1]


class _FakeSession:
    def __init__(self, table, memory):
        self.symbols, self._pyboy = _FakeSymbols(table), _FakePyboy(memory)


def _continuation_session(version, mutate=None):
    pins = _PROVIDER["yellow" if version == "yellow" else "red"]
    table = {
        "SelectEnemyMove": (15, pins["select"]),
        "LinkBattleExchangeData": (15, pins["exchange"]),
        "LoadScreenTilesFromBuffer1": (0, pins["load"]),
        "wSerialExchangeNybbleReceiveData": (0, 0xCC3E),
        "FullyParalyzedText": (15, pins["text"]),
        "PrintText": (0, pins["printer"]),
        "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed": (15, pins["player"]),
        "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed": (15, pins["enemy"]),
    }
    memory = _Mem()

    def put(bank, address, data):
        for index, value in enumerate(data):
            memory[bank, address + index] = value

    le = lambda value: value.to_bytes(2, "little")
    put(
        15,
        pins["select"] + 10,
        b"\xcd" + le(pins["exchange"]) + b"\xcd" + le(pins["load"]) + b"\xfa" + le(0xCC3E),
    )
    for key in ("player", "enemy"):
        put(15, pins[key] - 6, b"\x21" + le(pins["text"]) + b"\xcd" + le(pins["printer"]))
    for (pinned_version, _hook), (label, anchor, address, signature) in FROZEN_PINS_BY.items():
        if pinned_version == version:
            table[label] = (15, anchor)
            put(15, address, bytes.fromhex(signature))
    if mutate is not None:
        mutate(table, memory)
    return _FakeSession(table, memory)


# --- #595: real-observer two-peer KO/terminal flows (N3, N4) -------------------
# Canonical scripts: step keys name a hook event (suffix "#n" = write variant); each peer runs
# against its own fake session through the production installers (see _run_flow).
_WRAM = """
red: wPlayerMonNumber=cc2f wSerialExchangeNybbleReceiveData=cc3e
    wSerialExchangeNybbleSendData=cc42 wPlayerSelectedMove=ccdc wBattleResult=cf0b
    wEnemyMonSpecies=cfe5 wEnemyMonHP=cfe6 wEnemyMonPartyPos=cfe8 wEnemyMonStatus=cfe9
    wEnemyMonMoves=cfed wEnemyMonMaxHP=cff4 wEnemyMonPP=cffe wBattleMonSpecies=d014
    wBattleMonHP=d015 wBattleMonStatus=d018 wBattleMonMoves=d01c wBattleMonMaxHP=d023
    wBattleMonPP=d02d wIsInBattle=d057 wMoveMissed=d05f wDamage=d0d7 wLinkState=d12b
    wPartyCount=d163 wPartySpecies=d164 wPartyMons=d16b wCurMap=d35e hWhoseTurn=fff3
    wEnemySelectedMove=ccdd
yellow: wPlayerMonNumber=cc2f wSerialExchangeNybbleReceiveData=cc3e
    wSerialExchangeNybbleSendData=cc42 wPlayerSelectedMove=ccdc wBattleResult=cf0b
    wEnemyMonSpecies=cfe4 wEnemyMonHP=cfe5 wEnemyMonPartyPos=cfe7 wEnemyMonStatus=cfe8
    wEnemyMonMoves=cfec wEnemyMonMaxHP=cff3 wEnemyMonPP=cffd wBattleMonSpecies=d013
    wBattleMonHP=d014 wBattleMonStatus=d017 wBattleMonMoves=d01b wBattleMonMaxHP=d022
    wBattleMonPP=d02c wIsInBattle=d056 wMoveMissed=d05e wDamage=d0d6 wLinkState=d12a
    wPartyCount=d162 wPartySpecies=d163 wPartyMons=d16a wCurMap=d35d hWhoseTurn=fff3
    wEnemySelectedMove=ccdd
"""


_HOOKS = """
red: MainInBattleLoop=0f:4233 LinkBattleExchangeData=0f:5605 post_exchange=0f:5574
    ExecutePlayerMove=0f:565e PlayerCanExecuteMove=0f:56b0 DecrementPP=1a:4000
    ApplyDamageToEnemyPokemon=0f:6142 ApplyAttackToEnemyPokemonDone=0f:619d
    player_action_ko_return=0f:57c8 HandleEnemyMonFainted=0f:4525 terminal_victory=0f:4699
    EndOfBattle=04:77aa ReturnToCableClubRoom=01:577d ExecuteEnemyMove=0f:66bc
    EnemyCanExecuteMove=0f:672b ApplyDamageToPlayerPokemon=0f:6200
    ApplyAttackToPlayerPokemonDone=0f:625b enemy_action_ko_return=0f:684d
    HandlePlayerMonFainted=0f:4700 terminal_blackout=0f:4837 ExecuteEnemyMoveDone=0f:688c
    ExecutePlayerMoveDone=0f:580a
yellow: MainInBattleLoop=0f:4249 LinkBattleExchangeData=0f:5777 post_exchange=0f:56e6
    ExecutePlayerMove=0f:57d0 PlayerCanExecuteMove=0f:5822 DecrementPP=3d:42db
    ApplyDamageToEnemyPokemon=0f:62b4 ApplyAttackToEnemyPokemonDone=0f:630f
    player_action_ko_return=0f:593a HandleEnemyMonFainted=0f:453b terminal_victory=0f:46bb
    EndOfBattle=04:7765 ReturnToCableClubRoom=01:581e ExecuteEnemyMove=0f:6842
    EnemyCanExecuteMove=0f:68b1 ApplyDamageToPlayerPokemon=0f:6372
    ApplyAttackToPlayerPokemonDone=0f:63cd enemy_action_ko_return=0f:69d3
    HandlePlayerMonFainted=0f:471d terminal_blackout=0f:489c ExecuteEnemyMoveDone=0f:6a12
    ExecutePlayerMoveDone=0f:597c
"""


_STEPS = """
MainInBattleLoop | - > -
LinkBattleExchangeData | - > -
post_exchange | wSerialExchangeNybbleSendData=0 wSerialExchangeNybbleReceiveData=0
    wPlayerSelectedMove=1 > -
ExecutePlayerMove | hWhoseTurn=0 > -
PlayerCanExecuteMove | - > -
DecrementPP | hWhoseTurn=0 > wBattleMonPP=9,0,0,0
ApplyDamageToEnemyPokemon | wEnemyMonHP=0,35 wDamage=0,35 > wEnemyMonHP=0,0
ApplyAttackToEnemyPokemonDone | wMoveMissed=0 > -
player_action_ko_return | hWhoseTurn=0 wMoveMissed=0 > -
HandleEnemyMonFainted | - > -
ROM_STORE_wBattleResult | - > wBattleResult=0
terminal_victory | - > -
EndOfBattle | - > -
ReturnToCableClubRoom | wIsInBattle=0 wLinkState=1 wCurMap=240 > -
ExecuteEnemyMove | hWhoseTurn=1 > -
EnemyCanExecuteMove | - > -
ApplyDamageToPlayerPokemon | wBattleMonHP=0,35 wDamage=0,35 > wBattleMonHP=0,0
ApplyAttackToPlayerPokemonDone | wMoveMissed=0 > -
enemy_action_ko_return | hWhoseTurn=1 wMoveMissed=0 > -
HandlePlayerMonFainted | - > -
ROM_STORE_wBattleResult#2 | - > wBattleResult=1
terminal_blackout | - > -
ApplyDamageToPlayerPokemon#2 | wBattleMonHP=0,40 wDamage=0,5 > wBattleMonHP=0,35
ExecuteEnemyMoveDone | wMoveMissed=0 > -
ApplyDamageToEnemyPokemon#2 | wEnemyMonHP=0,40 wDamage=0,5 > wEnemyMonHP=0,35
ExecutePlayerMoveDone | wMoveMissed=0 > -
player_action_ko_return#2 | hWhoseTurn=0 wMoveMissed=0 wEnemyMonHP=0,0 > -
player_action_ko_return#3 | hWhoseTurn=1 wMoveMissed=0 > -
player_action_ko_return#4 | hWhoseTurn=0 wMoveMissed=1 > -
ApplyDamageToEnemyPokemon#3 | wEnemyMonHP=0,35 wDamage=0,34 > wEnemyMonHP=0,1
"""


_SCRIPTS = """
S00: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S01: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout
    EndOfBattle ReturnToCableClubRoom
S02: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon#2 ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return ExecuteEnemyMoveDone ExecutePlayerMove PlayerCanExecuteMove
    DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S03: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon#2 ApplyAttackToEnemyPokemonDone
    player_action_ko_return ExecutePlayerMoveDone ExecuteEnemyMove EnemyCanExecuteMove
    ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone enemy_action_ko_return
    HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout EndOfBattle
    ReturnToCableClubRoom
S04: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory EndOfBattle
    ReturnToCableClubRoom
S05: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP player_action_ko_return#2 HandleEnemyMonFainted
    ROM_STORE_wBattleResult terminal_victory EndOfBattle ReturnToCableClubRoom
S06: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon player_action_ko_return
    HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory EndOfBattle
    ReturnToCableClubRoom
S07: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return#3 HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S08: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return#4 HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S09: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon#3 ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S10: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return player_action_ko_return HandleEnemyMonFainted
    ROM_STORE_wBattleResult terminal_victory EndOfBattle ReturnToCableClubRoom
S11: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle ReturnToCableClubRoom
S12: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    terminal_victory EndOfBattle ReturnToCableClubRoom
S13: MainInBattleLoop terminal_blackout ROM_STORE_wBattleResult#2 EndOfBattle
    ReturnToCableClubRoom
S14: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult MainInBattleLoop
S15: MainInBattleLoop LinkBattleExchangeData post_exchange ExecutePlayerMove
    PlayerCanExecuteMove DecrementPP ApplyDamageToEnemyPokemon ApplyAttackToEnemyPokemonDone
    player_action_ko_return HandleEnemyMonFainted ROM_STORE_wBattleResult terminal_victory
    EndOfBattle
S16: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult#2 terminal_blackout
S17: MainInBattleLoop LinkBattleExchangeData post_exchange ExecuteEnemyMove
    EnemyCanExecuteMove ApplyDamageToPlayerPokemon ApplyAttackToPlayerPokemonDone
    enemy_action_ko_return HandlePlayerMonFainted ROM_STORE_wBattleResult terminal_blackout
    EndOfBattle ReturnToCableClubRoom
"""


_N3 = """
red-peer0-attacker-first red 0 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
red-peer0-attacker-second red 0 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
red-peer1-attacker-first red 1 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
red-peer1-attacker-second red 1 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
yellow-peer0-attacker-first yellow 0 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
yellow-peer0-attacker-second yellow 0 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
yellow-peer1-attacker-first yellow 1 S00 S01 9,11,12,13,-,13 8,10,11,12,-,12
yellow-peer1-attacker-second yellow 1 S02 S03 15,17,18,19,-,19 15,17,18,19,-,19
"""


_N4 = """
ko-flow-without-ko-hook | S04 | S01 | A | terminal_victory:terminal branch before settled
    action evidence | = | -,-,-,-,10,10 | 8,10,11,12,-,12
ko-completion-without-damage | S05 | S01 | A | player_action_ko_return:KO completion without
    damage application | = | -,-,-,-,7,7 | 8,10,11,12,-,12
ko-completion-pending-damage | S06 | S01 | A | player_action_ko_return:KO completion with
    pending damage application | = | -,-,-,-,8,8 | 8,10,11,12,-,12
ko-completion-wrong-turn-owner | S07 | S01 | A | player_action_ko_return:KO completion turn
    mismatch | = | -,-,-,-,9,9 | 8,10,11,12,-,12
ko-completion-miss-flag-contradiction | S08 | S01 | A | player_action_ko_return:KO completion
    contradicts miss flag | = | -,-,-,-,9,9 | 8,10,11,12,-,12
ko-hook-nonzero-target-ignored | S09 | S01 | A | terminal_victory:terminal branch before
    settled action evidence | = | -,-,-,-,11,11 | 8,10,11,12,-,12
ko-completion-duplicate | S10 | S01 | A | player_action_ko_return:KO completion repeated | = |
    9,-,-,-,10,10 | 8,10,11,12,-,12
ko-missing-pp-decrement | S11 | S01 | A | terminal_victory:executed move lacks exactly one
    local PP decrement | = | 8,-,-,-,10,10 | 8,10,11,12,-,12
duplicate-terminal-hook-after-settlement | S12 | S01 | - | - | - | 9,11,13,14,-,14 |
    8,10,11,12,-,12
blackout-before-exchange | S00 | S13 | - | - | settled snapshot is missing | 9,11,12,13,-,13 |
    -,-,3,4,-,4
nonterminal-ko-replacement | S14 | S01 | - | - | KO outcome lacks EndOfBattle evidence |
    9,11,-,-,-,11 | 8,10,11,12,-,12
missing-cleanup | S15 | S01 | - | - | battle cleanup is incomplete | 9,11,12,-,-,12 |
    8,10,11,12,-,12
ko-turn-without-end-of-battle-on-peer | S00 | S16 | - | - | KO outcome lacks EndOfBattle
    evidence | 9,11,12,13,-,13 | 8,10,-,-,-,10
noncomplementary-result-0-0 | S00 | S17 | - | - | battle terminal results are not
    complementary | 9,11,12,13,-,13 | 8,10,11,12,-,12
"""


def _blocks(text):
    lines = []
    for raw in text.strip().splitlines():
        if raw[:1] == " " and lines:
            lines[-1] += " " + raw.strip()
        else:
            lines.append(raw.strip())
    return lines


def _writes(text):
    pairs = [item.split("=") for item in ([] if text == "-" else text.split())]
    return [(symbol, [int(part) for part in data.split(",")]) for symbol, data in pairs]


def _parse_flow_tables():
    tables = {"wram": {}, "hooks": {}, "steps": {}, "scripts": {}}
    for kind, text in (("wram", _WRAM), ("hooks", _HOOKS)):
        for line in _blocks(text):
            version, _, items = line.partition(": ")
            tables[kind][version] = dict(item.split("=") for item in items.split())
    for line in _blocks(_STEPS):
        key, _, body = line.partition(" | ")
        before, _, after = body.partition(" > ")
        tables["steps"][key] = (_writes(before), _writes(after))
    for line in _blocks(_SCRIPTS):
        name, _, keys = line.partition(": ")
        tables["scripts"][name] = keys.split()
    return tables


_FLOW = _parse_flow_tables()


_N3_CASES = tuple(tuple(line.split()) for line in _blocks(_N3))


_N4_CASES = tuple(tuple(part.strip() for part in line.split(" | ")) for line in _blocks(_N4))


_CONTINUATION_EVENTS = {
    "post_exchange",
    "player_action_ko_return",
    "enemy_action_ko_return",
    "terminal_victory",
    "terminal_blackout",
}


_PEER_MONS = (((25, 40), (54, 35)), ((54, 35), (25, 40)))  # attacker, defender: (local, enemy)


def _poke(session, symbol, data):
    address = session.symbols.addr_of(symbol)
    for offset, value in enumerate(data):
        where = address + offset
        session._pyboy.memory[(1, where) if 0xD000 <= where <= 0xDFFF else where] = value


def _peer_session(version, local, enemy):
    """Independent fake session (own symbols, memory and hook recorder) in the plan's start state."""
    session = _continuation_session(version)
    table, memory = session.symbols.table, session._pyboy.memory
    for symbol, address in _FLOW["wram"][version].items():
        table[symbol] = (0, int(address, 16))
    for name, where in _FLOW["hooks"][version].items():
        if name not in _CONTINUATION_EVENTS:
            table[name] = tuple(int(part, 16) for part in where.split(":"))
    table["Moves"] = (14, 0x4000)
    for offset, value in enumerate((1, 0, 40, 100, 35, 0)):
        memory[14, 0x4000 + offset] = value
    record = bytearray(44)
    record[0], record[1:3], record[8:12] = local[0], bytes((0, local[1])), bytes((1, 0, 0, 0))
    record[29:33], record[34:36] = bytes((10, 0, 0, 0)), bytes((0, local[1]))
    start = {
        "wPartyCount": [1],
        "wPartySpecies": [local[0], 255],
        "wPartyMons": list(record),
        "wBattleMonSpecies": [local[0]],
        "wBattleMonHP": [0, local[1]],
        "wBattleMonMaxHP": [0, local[1]],
        "wBattleMonStatus": [0],
        "wBattleMonMoves": [1, 0, 0, 0],
        "wBattleMonPP": [10, 0, 0, 0],
        "wPlayerMonNumber": [0],
        "wEnemyMonSpecies": [enemy[0]],
        "wEnemyMonHP": [0, enemy[1]],
        "wEnemyMonMaxHP": [0, enemy[1]],
        "wEnemyMonStatus": [0],
        "wEnemyMonMoves": [1, 0, 0, 0],
        "wEnemyMonPP": [10, 0, 0, 0],
        "wEnemyMonPartyPos": [0],
        "wBattleResult": [0],
        "wMoveMissed": [0],
        "wDamage": [0, 0],
        "hWhoseTurn": [0],
        "wIsInBattle": [2],
        "wLinkState": [4],
        "wCurMap": [240],
    }
    for symbol, data in start.items():
        _poke(session, symbol, data)
    return session


def _play(version, session, observer, script):
    """Run one peer's script only through the hook callbacks recorded at the pinned locations."""
    completed = None
    for key in _FLOW["scripts"][script]:
        before, after = _FLOW["steps"][key]
        name = key.partition("#")[0]
        for symbol, data in before:
            _poke(session, symbol, data)
        if not name.startswith("ROM_STORE"):
            where = tuple(int(part, 16) for part in _FLOW["hooks"][version][name].split(":"))
            callbacks = session._pyboy.callbacks.get(where, [])
            assert callbacks, f"{name} has no registered callback at {where}"
            was_done = observer.ko_completed
            for callback in callbacks:
                callback(None)
            if observer.ko_completed and not was_done:
                completed = observer.sequence
        for symbol, data in after:
            _poke(session, symbol, data)
    return completed


def _run_flow(version, scripts, peer=0):
    """Production installer on two independent sessions; returns role-ordered results and rows."""
    from tests._pyboy_link_session_roms_battle_support import _install_battle_diag_counters

    order = (0, 1) if peer == 0 else (1, 0)
    sessions = [_peer_session(version, *_PEER_MONS[role]) for role in order]
    observers = _install_battle_diag_counters(*sessions, versions=(version, version))
    observers = observers["_battle_evidence"]
    completed = [None, None]
    for position, role in enumerate(order):
        completed[role] = _play(version, sessions[position], observers[position], scripts[role])
    by_role = [observers[order.index(role)] for role in (0, 1)]
    return by_role, completed, [observer.snapshot() for observer in observers]


def _check_peer(observer, completion, anchors, error=None):
    done, settled, ended, cleaned, failed, visits = (
        None if part == "-" else int(part) for part in anchors.split(",")
    )
    row = observer.snapshot()
    assert (observer.sequence, completion) == (visits, done)
    if failed is not None:
        assert observer.error == error and observer.sequence == failed and row["settled"] is False
        return
    assert observer.error is None and row["settled"] is (settled is not None)
    if settled is not None:
        assert row["settled_seq"] == settled
    assert (row.get("terminal") or {}).get("seq") == ended
    assert (row.get("cleanup") or {}).get("seq") == cleaned


_HP_GUARD = (
    '        if _read(self.session, "wEnemyMonHP" if side == "local" else "wBattleMonHP", 2)'
    " != [0, 0]:\n            return\n"
)


# case id -> (target, symbol, exact source anchor, replacement, first error, KO completion seq, visits)
_MUTANTS = {
    "ko-hook-nonzero-target-ignored": (
        evidence.BattleTurnObserver,
        "_ko_return",
        _HP_GUARD,
        "",
        "terminal_victory:faint skip actor is not fainted",
        9,
        11,
    ),
    "ko-missing-pp-decrement": (
        evidence,
        "validate_turn",
        "entries != 1 or ",
        "",
        "terminal_victory:local PP delta is inconsistent with execution",
        8,
        10,
    ),
}


def _run_mutant(monkeypatch, name, scripts, baseline_error):
    """Re-run the case on fresh sessions with ONE production fragment removed in memory."""
    target, symbol, anchor, replacement, cause, completion, visits = _MUTANTS[name]
    hook, _, reason = cause.partition(":")
    mutant_error = f"battle observation {hook}: ValueError: {reason}"
    assert mutant_error != baseline_error
    with source_mutant(monkeypatch, target, symbol, anchor, replacement):
        observers, completed, rows = _run_flow("red", scripts)
        assert verify_battle_turns(rows) == [mutant_error]
        attacker, defender = observers
        assert (attacker.error, attacker.sequence, completed[0]) == (
            mutant_error,
            visits,
            completion,
        )
        assert (defender.error, completed[1]) == (None, 8)
    clean_observers, _, clean_rows = _run_flow("red", scripts)
    assert verify_battle_turns(clean_rows) == [baseline_error]
    assert clean_observers[0].error == baseline_error
