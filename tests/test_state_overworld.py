from __future__ import annotations

import pytest

from pokered_harness.state.base import Direction, parse_direction
from pokered_harness.state.overworld import BIT_SCRIPTED_MOVEMENT_STATE, parse_overworld
from pokered_harness.symbols.loader import load_sym_text


def test_parse_direction_canonical():
    assert parse_direction(0x00) is Direction.DOWN
    assert parse_direction(0x04) is Direction.UP
    assert parse_direction(0x08) is Direction.LEFT
    assert parse_direction(0x0C) is Direction.RIGHT


def test_parse_direction_returns_none_for_invalid():
    assert parse_direction(0x01) is None
    assert parse_direction(0xFF) is None


def test_overworld_standing_at_origin_of_pallet(mem, symbols):
    # Pallet Town in pokered is MAP_PALLET_TOWN = 0x00; coords start (5, 6).
    mem[0xD35E] = 0x00  # wCurMap
    mem[0xD362] = 0x05  # wXCoord
    mem[0xD361] = 0x06  # wYCoord
    mem[0xD46A] = 0x00  # wWalkCounter — standing still
    mem[0xC109] = 0x00  # facing DOWN

    state = parse_overworld(mem, symbols)

    assert state.map_id == 0x00
    assert state.x == 5
    assert state.y == 6
    assert state.walk_counter == 0
    assert state.is_standing is True
    assert state.direction is Direction.DOWN
    assert state.scripted_movement_active is False


def test_overworld_reads_fields_from_loaded_symbol_addresses(mem):
    # Use addresses that differ from the usual pokered layout. This proves
    # that map, position, movement, and facing state come from the loaded
    # symbol table rather than from guessed Red/Blue/Yellow addresses.
    sym = load_sym_text(
        """
        00:C200 wCurMap
        00:C201 wYCoord
        00:C202 wXCoord
        00:C203 wWalkCounter
        00:C204 wSpritePlayerStateData1FacingDirection
        00:C205 wCurrentMapScriptFlags
        00:C206 wCurMapScript
        00:C207 wStatusFlags5
        """
    )
    mem[0xC200] = 0xFF  # raw map byte; no map table is available here
    mem[0xC201] = 0xFE  # raw Y byte; geometry is map-specific
    mem[0xC202] = 0xFD  # raw X byte; geometry is map-specific
    mem[0xC203] = 0xFF  # any non-zero counter means not standing
    mem[0xC204] = 0x0C  # RIGHT
    mem[0xC205] = 0xA5
    mem[0xC206] = 0x07
    mem[0xC207] = 1 << BIT_SCRIPTED_MOVEMENT_STATE

    state = parse_overworld(mem, sym)

    assert state.map_id == 0xFF
    assert state.x == 0xFD
    assert state.y == 0xFE
    assert state.walk_counter == 0xFF
    assert state.is_standing is False
    assert state.direction is Direction.RIGHT
    assert state.map_script_flags == 0xA5
    assert state.current_map_script == 0x07
    assert state.status_flags5_raw == 1 << BIT_SCRIPTED_MOVEMENT_STATE
    assert state.scripted_movement_active is True


def test_overworld_mid_step_not_standing(mem, symbols):
    mem[0xD46A] = 0x04  # mid-tile animation frame
    state = parse_overworld(mem, symbols)
    assert state.walk_counter == 4
    assert state.is_standing is False


@pytest.mark.parametrize(
    ("raw_flags", "active"),
    (
        (0x00, False),
        # wStatusFlags5 is a u8. Bit 7 is the highest representable bit, so
        # there is no valid ``BIT_SCRIPTED_MOVEMENT_STATE + 1`` case.
        (0x01, False),
        (1 << (BIT_SCRIPTED_MOVEMENT_STATE - 1), False),
        (1 << BIT_SCRIPTED_MOVEMENT_STATE, True),
        (0xFF, True),
    ),
)
def test_overworld_scripted_movement_flag(mem, symbols, raw_flags, active):
    mem[0xD7D4] = raw_flags
    state = parse_overworld(mem, symbols)
    assert state.status_flags5_raw == raw_flags
    assert state.scripted_movement_active is active


def test_overworld_invalid_direction_is_unknown(mem, symbols):
    # A partially updated sprite direction is not a movement direction and
    # must not be guessed from a nearby value.
    mem[0xC109] = 0x01
    state = parse_overworld(mem, symbols)
    assert state.direction is None


@pytest.mark.parametrize("missing", ("wCurMap", "wXCoord", "wYCoord", "wWalkCounter"))
def test_overworld_missing_core_symbol_is_not_guessed(mem, missing):
    # DictMemory returns zero for unset addresses. Missing symbol metadata is
    # different from a real zero byte and must fail closed instead of
    # fabricating Pallet Town, coordinate (0, 0), or a standing state.
    lines = (
        "00:D35E wCurMap",
        "00:D361 wYCoord",
        "00:D362 wXCoord",
        "00:D46A wWalkCounter",
    )
    sym = load_sym_text("\n".join(line for line in lines if not line.endswith(f" {missing}")))

    with pytest.raises(KeyError):
        parse_overworld(mem, sym)


def test_overworld_direction_fallback_via_sprite_state_base(mem):
    # No wSpritePlayerStateData1FacingDirection symbol — fall back to
    # wSpriteStateData1 + 9.
    sym = load_sym_text(
        """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:D46A wWalkCounter
        00:C100 wSpriteStateData1
        """
    )
    mem[0xC100 + 9] = 0x08  # LEFT
    state = parse_overworld(mem, sym)
    assert state.direction is Direction.LEFT


def test_overworld_direction_none_when_no_symbol_available(mem):
    sym = load_sym_text(
        """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:D46A wWalkCounter
        """
    )
    state = parse_overworld(mem, sym)
    assert state.direction is None


def test_overworld_optional_symbols_return_none_when_missing(mem):
    sym = load_sym_text(
        """
        00:D35E wCurMap
        00:D361 wYCoord
        00:D362 wXCoord
        00:D46A wWalkCounter
        """
    )
    state = parse_overworld(mem, sym)
    assert state.map_script_flags is None
    assert state.current_map_script is None
    assert state.status_flags5_raw is None
    assert state.scripted_movement_active is False
