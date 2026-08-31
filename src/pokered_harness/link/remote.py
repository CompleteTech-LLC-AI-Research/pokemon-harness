"""Two-process link-cable endpoint.

One ``RemoteLinkEndpoint`` lives in each MCP/Pokemon-harness process.
Both endpoints communicate over a :class:`SerialLink` (TCP between
agents; in-process queue for tests). Unlike :class:`LinkPair` — which
owns *both* sessions in a single process and steps them in lockstep —
each endpoint owns *one* :class:`Session` and synchronously exchanges
bytes with its peer only when the game's serial routines fire.

Cross-version correctness: at hook time we resolve the register ``hl``
(the game's pointer into a serial buffer) to a *symbol name* via the
local :class:`SymbolTable`, then use that symbol as the RPC ``kind``.
The peer does the same on its side with its own symbol table — Yellow
maps the symbol to a different physical address than Blue, but the
wire byte format is identical so the exchange is correct.

Clock-role negotiation: whoever calls :meth:`TcpSerialLink.listen` is
treated as the internal-clock master (status byte = 0x02); whoever
:meth:`connect`\\s is the external-clock slave (status byte = 0x01).
See pret/pokered constants/serial_constants.asm.

What this class deliberately does NOT do
----------------------------------------

Two features of the in-process :class:`LinkPair` are intentionally
dropped in the remote model — each is an automation convenience
that only made sense when one process drove both players:

- **LinkMenu auto-select TRADE.** The agent drives their own menu
  selection via normal MCP button-press tools.
- **Hidden-event auto-trigger in TRADE_CENTER.** The agent walks to
  the table and presses A themselves.

What stays:

- **Clock-role handshake** (writing ``hSerialConnectionStatus`` on our
  side when the game calls ``Serial_TryEstablishingExternallyClockedConnection``).
- **hardware serial tick** (clearing SC_START and raising IF-bit 3 so
  the Serial ISR fires). Purely local behaviour.
- **Semantic byte exchanges** for ``Serial_ExchangeBytes``,
  ``Serial_ExchangeNybble``, ``Serial_ExchangeLinkMenuSelection`` —
  each hook now blocks on :meth:`SerialLink.exchange` instead of
  reading the peer's in-process memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pokered_harness.link.serial_link import SerialLink, SerialLinkTimeout

if TYPE_CHECKING:
    from pokered_harness.session import Session


# --- clock role constants -------------------------------------------------


#: ``hSerialConnectionStatus`` byte announcing the peer-drives-clock role.
STATUS_EXTERNAL: int = 0x01  # connector side (the one that calls connect())
#: ``hSerialConnectionStatus`` byte announcing the we-drive-clock role.
STATUS_INTERNAL: int = 0x02  # listener side


# --- hardware serial constants (GB common, not pret-specific) -------------


_RSC_ADDR: int = 0xFF02
_IF_ADDR: int = 0xFF0F
_SC_START: int = 0x80
_IF_SERIAL: int = 0x08


# --- endpoint --------------------------------------------------------------


class RemoteLinkEndpoint:
    """One side of a two-agent link-cable pair.

    Build with :meth:`as_listener` (clock master / INTERNAL) or
    :meth:`as_connector` (clock slave / EXTERNAL). Call :meth:`install`
    once to register the hooks; after that, the local session's normal
    ``step`` / ``press`` calls will transparently block on serial
    exchanges when the game's link routines fire.

    Typical flow in an MCP server::

        link = TcpSerialLink.connect("peer.host", 9999, "blue")
        endpoint = RemoteLinkEndpoint.as_connector(session, link)
        endpoint.install()
        # session.press('a'), session.step(...) as normal — the
        # endpoint's hooks handle serial exchanges in the background.
    """

    def __init__(
        self,
        session: Session,
        serial_link: SerialLink,
        *,
        is_internal_clock: bool,
    ) -> None:
        self._session = session
        self._link = serial_link
        self._is_internal_clock = is_internal_clock
        self._installed = False

    # --- construction -------------------------------------------------

    @classmethod
    def as_listener(
        cls, session: Session, serial_link: SerialLink
    ) -> RemoteLinkEndpoint:
        """The peer that called ``TcpSerialLink.listen`` — drives the
        clock (status = USING_INTERNAL_CLOCK 0x02)."""
        return cls(session, serial_link, is_internal_clock=True)

    @classmethod
    def as_connector(
        cls, session: Session, serial_link: SerialLink
    ) -> RemoteLinkEndpoint:
        """The peer that called ``TcpSerialLink.connect`` — follows the
        clock (status = USING_EXTERNAL_CLOCK 0x01)."""
        return cls(session, serial_link, is_internal_clock=False)

    # --- public surface -----------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    @property
    def serial_link(self) -> SerialLink:
        return self._link

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def clock_status_byte(self) -> int:
        return STATUS_INTERNAL if self._is_internal_clock else STATUS_EXTERNAL

    def install(self) -> None:
        """Register all bridge hooks on the local session."""
        if self._installed:
            raise RuntimeError("RemoteLinkEndpoint already installed")
        self._install_handshake()
        self._install_exchange_nybble()
        self._install_exchange_link_menu_selection()
        self._install_exchange_bytes_skip()
        self._installed = True

    def serial_tick(self) -> None:
        """Clear SC_START and raise IF-bit 3 on the local session so
        the Serial ISR fires.

        Called by the MCP server's step loop after each emulator frame
        (analogous to :meth:`LinkPair._hardware_serial_tick`). Purely
        local — no peer interaction."""
        mem = self._session._pyboy.memory
        sc = mem[_RSC_ADDR]
        if not (sc & _SC_START):
            return
        mem[_RSC_ADDR] = sc & ~_SC_START
        mem[_IF_ADDR] = mem[_IF_ADDR] | _IF_SERIAL

    def step(self, count: int = 1, *, render: bool = False) -> None:
        """Advance the compatibility endpoint one frame at a time.

        The semantic fallback has no native PyBoy serial backend.  Its
        hardware-serial tick must therefore run after every emulator frame;
        batching several frames before calling :meth:`serial_tick` can let
        the ROMs enter different serial phases and strand a peer exchange.
        Native ``NetworkBackend`` sessions do not use this method because
        their serial edges are serviced by the backend itself.
        """
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError(f"count must be a positive integer, got {count!r}")
        for _ in range(count):
            self._session.step(1, render=render)
            self.serial_tick()

    # --- hook installers ----------------------------------------------

    def _install_handshake(self) -> None:
        """Set ``hSerialConnectionStatus`` to our clock-role byte
        whenever the game's handshake routine fires."""
        session = self._session
        if "Serial_TryEstablishingExternallyClockedConnection" not in session.symbols:
            return
        status_addr = session.symbols.addr_of("hSerialConnectionStatus")
        my_status = self.clock_status_byte
        mem = session._pyboy.memory

        def _cb(_ctx) -> None:
            mem[status_addr] = my_status

        try:
            session.serial_hook(
                "Serial_TryEstablishingExternallyClockedConnection", _cb
            )
        except (KeyError, LookupError):
            pass

    def _install_exchange_nybble(self) -> None:
        """Intercept ``Serial_ExchangeNybble`` and exchange the 1-byte
        ``wSerialExchangeNybbleSendData`` with the peer via RPC."""
        session = self._session
        if "Serial_ExchangeNybble" not in session.symbols:
            return
        if "wSerialExchangeNybbleSendData" not in session.symbols:
            return
        send_addr = session.symbols.addr_of("wSerialExchangeNybbleSendData")
        recv_addr = session.symbols.addr_of("wSerialExchangeNybbleReceiveData")
        mem = session._pyboy.memory
        link = self._link

        def _cb(_ctx) -> None:
            my_byte = bytes([mem[send_addr] & 0xFF])
            try:
                peer_bytes = link.exchange(
                    "exchange_nybble/wSerialExchangeNybbleSendData", my_byte
                )
            except SerialLinkTimeout:
                return  # leave recv cell untouched; game will retry/fail
            if peer_bytes:
                mem[recv_addr] = peer_bytes[0] & 0xFF

        session.serial_hook("Serial_ExchangeNybble", _cb)

    def _install_exchange_link_menu_selection(self) -> None:
        """Intercept ``Serial_ExchangeLinkMenuSelection`` and exchange
        the 2-byte menu-selection send buffer."""
        session = self._session
        if "Serial_ExchangeLinkMenuSelection" not in session.symbols:
            return
        if "wLinkMenuSelectionSendBuffer" not in session.symbols:
            return
        send_addr = session.symbols.addr_of("wLinkMenuSelectionSendBuffer")
        recv_addr = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
        mem = session._pyboy.memory
        link = self._link

        def _cb(_ctx) -> None:
            my_bytes = bytes([mem[send_addr] & 0xFF, mem[send_addr + 1] & 0xFF])
            try:
                peer_bytes = link.exchange(
                    "menu_selection/wLinkMenuSelectionSendBuffer", my_bytes
                )
            except SerialLinkTimeout:
                return
            if len(peer_bytes) >= 2:
                mem[recv_addr] = peer_bytes[0] & 0xFF
                mem[recv_addr + 1] = peer_bytes[1] & 0xFF

        session.serial_hook("Serial_ExchangeLinkMenuSelection", _cb)

    def _install_exchange_bytes_skip(self) -> None:
        """Skip ``Serial_ExchangeBytes`` and exchange arbitrary-length
        buffer over the link.

        The call's ``hl`` register points at our send buffer; we
        resolve that address to a symbol name via the local
        :class:`SymbolTable` so the RPC ``kind`` is version-agnostic
        (Blue's ``wSerialPlayerDataBlock`` at ``0xD141`` and Yellow's
        at a different address both serialize as kind=
        ``"exchange_bytes/wSerialPlayerDataBlock"``). Peer does the
        same on their side — they resolve their own ``hl`` to their
        own address for that symbol.
        """
        session = self._session
        if "Serial_ExchangeBytes" not in session.symbols:
            return
        bank, addr = session.symbols.bank_addr("Serial_ExchangeBytes")
        mem = session._pyboy.memory
        pb = session._pyboy
        symbols = session.symbols
        link = self._link

        # Set of symbol names that refer to serial-buffer WRAM cells
        # the game calls Serial_ExchangeBytes with. We use these to
        # map a raw hl back to a symbol name.
        candidate_symbols = tuple(
            s
            for s in (
                "wSerialPlayerDataBlock",
                "wSerialRandomNumberListBlock",
                "wSerialPartyMonsPatchList",
                "wSerialEnemyDataBlock",
                "wSerialOtherGameboyRandomNumberListBlock",
                "wSerialEnemyMonsPatchList",
            )
            if s in symbols
        )
        candidate_addrs = {symbols.addr_of(s): s for s in candidate_symbols}

        def _cb(_ctx) -> None:
            rf = pb.register_file
            hl = rf.HL
            de = (rf.D << 8) | rf.E
            bc = (rf.B << 8) | rf.C
            if bc == 0:
                return
            # Resolve hl -> symbol. Game always calls with hl exactly at
            # a buffer start, so an exact address match is enough.
            symbol = candidate_addrs.get(hl)
            if symbol is None:
                # Fallback: ask the symbol table directly.
                names = symbols.names_at(0, hl)
                symbol = names[0] if names else f"anon_0x{hl:04x}"
            my_bytes = bytes(mem[hl + i] & 0xFF for i in range(bc))
            try:
                peer_bytes = link.exchange(
                    f"exchange_bytes/{symbol}", my_bytes
                )
            except SerialLinkTimeout:
                return  # leave buffer untouched; peer cable unplugged
            if len(peer_bytes) != bc:
                return  # protocol mismatch — drop rather than corrupt
            for i, b in enumerate(peer_bytes):
                mem[(de + i) & 0xFFFF] = b & 0xFF
            # Simulate RET by popping return addr off the stack.
            sp = rf.SP
            rf.PC = (mem[sp + 1] << 8) | mem[sp]
            rf.SP = (sp + 2) & 0xFFFF
            rf.HL = (hl + bc) & 0xFFFF
            new_de = (de + bc) & 0xFFFF
            rf.D = new_de >> 8
            rf.E = new_de & 0xFF
            rf.B = 0
            rf.C = 0
            # Pokered's Serial_ExchangeBytes final instructions are
            # `xor a` (A=0, Z=1) then `ret`. Set rf.A = 0 and
            # rf.F = 0x80 (Z flag in bit 7) so flag-conditional
            # branches after the simulated RET see the same state
            # as on real hardware.
            rf.A = 0
            rf.F = 0x80

        # Replace any pre-existing Serial_ExchangeBytes hook (e.g. the
        # SerialBridge BRIDGE-role callback) with our remote variant.
        try:
            pb.hook_deregister(bank, addr)
        except Exception:
            pass
        pb.hook_register(bank, addr, _cb, None)


__all__ = [
    "RemoteLinkEndpoint",
    "STATUS_EXTERNAL",
    "STATUS_INTERNAL",
]
