from __future__ import annotations

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


def test_overworld_mid_step_not_standing(mem, symbols):
    mem[0xD46A] = 0x04  # mid-tile animation frame
    state = parse_overworld(mem, symbols)
    assert state.walk_counter == 4
    assert state.is_standing is False


def test_overworld_scripted_movement_flag(mem, symbols):
    mem[0xD7D4] = 1 << BIT_SCRIPTED_MOVEMENT_STATE
    state = parse_overworld(mem, symbols)
    assert state.status_flags5_raw == 1 << BIT_SCRIPTED_MOVEMENT_STATE
    assert state.scripted_movement_active is True


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
