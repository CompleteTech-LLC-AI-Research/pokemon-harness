# #105 real-ROM terminal-evidence report

Generated 2026-09-21T12:09:00+00:00 by `make_pr105_report.py`.
Rows: 48 PASS / 48 expected, 0 non-pass, 0 in flight, 0 never started.
JUnit skipped cases among passing rows: 0.

**Verdict: PASS.**

## Pass by runtime

| runtime | pass | expected |
| --- | --- | --- |
| cython | 24 | 24 |
| source | 24 | 24 |

## Acceptance criteria mapping

| criterion | evidence |
| --- | --- |
| #105-1 full declared trade matrix with exact paired record evidence in both runtimes | all 48 rows PASS (24 rows x source/cython): 9 ordered local + 9 ordered TCP orientations, 2 multi-member slot rows, 3 EOF rows, 1 cancel row |
| #105-2 public record-observation contract documented, bounded, and tested for stale/invalid reads and owner isolation | unit tier on the candidate (tests/test_party_record_audit.py, tests/test_mcp_server.py) + docs; not satisfied by a real-ROM row |
| #105-3 report distinguishes completed trades, cancelled precommit flows and interrupted/failed trades | `cancel_before_commitment` row + 3 EOF rows + this report's per-row status; clean process exit alone is never counted as a trade |
| #105-4 every required scenario has terminal passing evidence, no skip/xfail/error/timeout/missing result counted as PASS | this tool's PASS rule (rc=0 AND junit tests>=1, failures=0, errors=0, skipped=0) plus the zero-skip count below |
| #105-5 failure/cancellation cases preserve the original error and prove bounded cleanup | the three EOF rows and the cancel row assert the preserved failure and owned-resource cleanup; a serial re-run is required for any of them that is not rc=0 here |
| #105-6 docs and executable coverage declaration state implemented scope and remaining exclusions; release status stays partial | PR #118 documentation + VERSIONS.md/README; verified by reading, not by a matrix row |

## Passing rows

| row | runtime | seconds | ended (UTC) | junit tests | junit sha256 |
| --- | --- | --- | --- | --- | --- |
| `source_test_real_rom_mcp_trade_eof_during_setup_exits_cleanly_red_color-blue_color_` | source | 31 | 2026-09-21T04:54:47+00:00 | 1 | `55afbb8725f9efb4` |
| `source_test_real_rom_mcp_trade_eof_during_active_trade_exits_cleanly_red_color-blue_color_` | source | 629 | 2026-09-21T05:19:28+00:00 | 1 | `376776fbe5c83d85` |
| `source_test_real_rom_mcp_trade_eof_after_commitment_exits_cleanly_red_color-blue_color_` | source | 717 | 2026-09-21T05:20:56+00:00 | 1 | `2734979742f7b11c` |
| `source_test_real_rom_mcp_trade_cancel_before_commitment_keeps_records_red_color-blue_color_` | source | 1680 | 2026-09-21T06:58:34+00:00 | 1 | `38b70b821679f1a3` |
| `source_test_real_rom_mcp_trade_exchanges_multi_member_slot_records_red_color-blue_color-out2-peerout0_` | source | 1657 | 2026-09-21T06:58:11+00:00 | 1 | `49e0257e8a052965` |
| `source_test_real_rom_mcp_trade_exchanges_multi_member_slot_records_blue_color-red_color-out0-peerout3_` | source | 1500 | 2026-09-21T07:23:11+00:00 | 1 | `2e66af6af850e6d5` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_red_color-red_color_` | source | 1508 | 2026-09-21T07:23:42+00:00 | 1 | `ab083289844c6881` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_red_color-blue_color_` | source | 1527 | 2026-09-21T07:48:38+00:00 | 1 | `2689a4203c2ea9d5` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_red_color-yellow_` | source | 1146 | 2026-09-21T07:42:48+00:00 | 1 | `58f8d3910997f785` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_blue_color-red_color_` | source | 1556 | 2026-09-21T08:08:44+00:00 | 1 | `f410e07c94782290` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_blue_color-blue_color_` | source | 1539 | 2026-09-21T08:14:17+00:00 | 1 | `a325c5788b8c4963` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_blue_color-yellow_` | source | 1619 | 2026-09-21T08:35:43+00:00 | 1 | `a7b3c4424a66438b` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_yellow-red_color_` | source | 1701 | 2026-09-21T08:42:38+00:00 | 1 | `f896ba1e48041608` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_yellow-blue_color_` | source | 1309 | 2026-09-21T08:57:32+00:00 | 1 | `608beb24432b1b43` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_yellow-yellow_` | source | 826 | 2026-09-21T08:56:24+00:00 | 1 | `f02bc32ba9159abb` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-red_color_` | source | 283 | 2026-09-21T09:01:07+00:00 | 1 | `0294a70d77908913` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-blue_color_` | source | 288 | 2026-09-21T09:02:20+00:00 | 1 | `a934fab68b2f1686` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-yellow_` | source | 296 | 2026-09-21T09:06:03+00:00 | 1 | `08f64d1de7ac0a51` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-red_color_` | source | 226 | 2026-09-21T09:06:06+00:00 | 1 | `f6a525b5b078131d` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-blue_color_` | source | 335 | 2026-09-21T09:11:38+00:00 | 1 | `5b4900ef57b8e29d` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-yellow_` | source | 307 | 2026-09-21T09:11:13+00:00 | 1 | `1894b23e0da83279` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-red_color_` | source | 206 | 2026-09-21T09:17:48+00:00 | 1 | `2905119c26d06af3` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-blue_color_` | source | 206 | 2026-09-21T09:17:48+00:00 | 1 | `3b9c05abe5955ed8` |
| `source_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-yellow_` | source | 259 | 2026-09-21T09:22:07+00:00 | 1 | `45cb4f673bf19516` |
| `cython_test_real_rom_mcp_trade_eof_during_setup_exits_cleanly_red_color-blue_color_` | cython | 2 | 2026-09-21T09:17:50+00:00 | 1 | `91e5801ddeef234c` |
| `cython_test_real_rom_mcp_trade_eof_during_active_trade_exits_cleanly_red_color-blue_color_` | cython | 86 | 2026-09-21T09:19:16+00:00 | 1 | `0d7cc267968aec3e` |
| `cython_test_real_rom_mcp_trade_eof_after_commitment_exits_cleanly_red_color-blue_color_` | cython | 108 | 2026-09-21T09:21:04+00:00 | 1 | `e85c7f61c7420aeb` |
| `cython_test_real_rom_mcp_trade_cancel_before_commitment_keeps_records_red_color-blue_color_` | cython | 434 | 2026-09-21T09:28:18+00:00 | 1 | `f522c2506bd2b7eb` |
| `cython_test_real_rom_mcp_trade_exchanges_multi_member_slot_records_red_color-blue_color-out2-peerout0_` | cython | 328 | 2026-09-21T10:52:24+00:00 | 1 | `1537bb1c5b12bc0b` |
| `cython_test_real_rom_mcp_trade_exchanges_multi_member_slot_records_blue_color-red_color-out0-peerout3_` | cython | 353 | 2026-09-21T10:58:17+00:00 | 1 | `94d18d2872c3b34e` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_red_color-red_color_` | cython | 328 | 2026-09-21T11:03:45+00:00 | 1 | `c1ad47ab711a87d6` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_red_color-blue_color_` | cython | 320 | 2026-09-21T11:09:05+00:00 | 1 | `c0cdfd54ba11c459` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_red_color-yellow_` | cython | 395 | 2026-09-21T11:15:40+00:00 | 1 | `90b9b57dbac933e9` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_blue_color-red_color_` | cython | 396 | 2026-09-21T11:22:17+00:00 | 1 | `151088986a5135e2` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_blue_color-blue_color_` | cython | 269 | 2026-09-21T11:26:46+00:00 | 1 | `b7f2e04aa60f52e8` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_blue_color-yellow_` | cython | 1498 | 2026-09-21T10:08:51+00:00 | 1 | `03b7df8c34c74ee7` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_yellow-red_color_` | cython | 200 | 2026-09-21T11:30:06+00:00 | 1 | `736336f4984fa020` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_yellow-blue_color_` | cython | 1228 | 2026-09-21T10:09:35+00:00 | 1 | `da094cca69f550d9` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_yellow-yellow_` | cython | 1330 | 2026-09-21T10:31:01+00:00 | 1 | `5334e13e623a120c` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-red_color_` | cython | 206 | 2026-09-21T11:33:32+00:00 | 1 | `64a4465db11fccd8` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-blue_color_` | cython | 207 | 2026-09-21T11:36:59+00:00 | 1 | `684efc94b27cb938` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_red_color-yellow_` | cython | 485 | 2026-09-21T10:39:06+00:00 | 1 | `12ecfd3b5546e08d` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-red_color_` | cython | 171 | 2026-09-21T10:35:38+00:00 | 1 | `f907c856f7ee5019` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-blue_color_` | cython | 267 | 2026-09-21T10:40:05+00:00 | 1 | `0d1fd412407f8fca` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_blue_color-yellow_` | cython | 251 | 2026-09-21T10:43:17+00:00 | 1 | `e9d5779ac1773dc0` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-red_color_` | cython | 44 | 2026-09-21T10:40:49+00:00 | 1 | `4eecbb4f56b305c0` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-blue_color_` | cython | 44 | 2026-09-21T10:41:33+00:00 | 1 | `d8ec153035414a67` |
| `cython_test_real_rom_mcp_trade_exchanges_party_records_over_tcp_yellow-yellow_` | cython | 241 | 2026-09-21T10:45:34+00:00 | 1 | `57b31d3620e47cb7` |

## Non-pass rows

None.
