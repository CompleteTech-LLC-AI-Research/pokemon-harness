def SWAP_131(cpu): # 131 SWAP C
    a = cpu.C
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_132(cpu): # 132 SWAP D
    a = cpu.D
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_133(cpu): # 133 SWAP E
    a = cpu.E
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_134(cpu): # 134 SWAP H
    a = (cpu.HL >> 8)
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_135(cpu): # 135 SWAP L
    a = (cpu.HL & 0xFF)
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_136(cpu): # 136 SWAP (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SWAP_137(cpu): # 137 SWAP A
    a = cpu.A
    t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_138(cpu): # 138 SRL B
    a = cpu.B
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.B = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_139(cpu): # 139 SRL C
    a = cpu.C
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.C = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13A(cpu): # 13A SRL D
    a = cpu.D
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.D = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13B(cpu): # 13B SRL E
    a = cpu.E
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.E = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13C(cpu): # 13C SRL H
    a = (cpu.HL >> 8)
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0x00FF) | (t << 8)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13D(cpu): # 13D SRL L
    a = (cpu.HL & 0xFF)
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.HL = (cpu.HL & 0xFF00) | (t & 0xFF)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13E(cpu): # 13E SRL (HL)
    cpu.cycles += 4
    a = cpu.mb.getitem(cpu.HL)
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.cycles += 4
    cpu.mb.setitem(cpu.HL, t)
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def SRL_13F(cpu): # 13F SRL A
    a = cpu.A
    t = (a >> 1) + ((a & 1) << 8)
    flag = 0b00000000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    flag |= (t > 0xFF) << FLAGC
    cpu.F &= 0b00000000
    cpu.F |= flag
    t &= 0xFF
    cpu.A = t
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_140(cpu): # 140 BIT 0,B
    t = cpu.B & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_141(cpu): # 141 BIT 0,C
    t = cpu.C & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_142(cpu): # 142 BIT 0,D
    t = cpu.D & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_143(cpu): # 143 BIT 0,E
    t = cpu.E & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_144(cpu): # 144 BIT 0,H
    t = (cpu.HL >> 8) & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_145(cpu): # 145 BIT 0,L
    t = (cpu.HL & 0xFF) & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_146(cpu): # 146 BIT 0,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_147(cpu): # 147 BIT 0,A
    t = cpu.A & (1 << 0)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_148(cpu): # 148 BIT 1,B
    t = cpu.B & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_149(cpu): # 149 BIT 1,C
    t = cpu.C & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14A(cpu): # 14A BIT 1,D
    t = cpu.D & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14B(cpu): # 14B BIT 1,E
    t = cpu.E & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14C(cpu): # 14C BIT 1,H
    t = (cpu.HL >> 8) & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14D(cpu): # 14D BIT 1,L
    t = (cpu.HL & 0xFF) & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14E(cpu): # 14E BIT 1,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_14F(cpu): # 14F BIT 1,A
    t = cpu.A & (1 << 1)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_150(cpu): # 150 BIT 2,B
    t = cpu.B & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_151(cpu): # 151 BIT 2,C
    t = cpu.C & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_152(cpu): # 152 BIT 2,D
    t = cpu.D & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_153(cpu): # 153 BIT 2,E
    t = cpu.E & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_154(cpu): # 154 BIT 2,H
    t = (cpu.HL >> 8) & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_155(cpu): # 155 BIT 2,L
    t = (cpu.HL & 0xFF) & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_156(cpu): # 156 BIT 2,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_157(cpu): # 157 BIT 2,A
    t = cpu.A & (1 << 2)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_158(cpu): # 158 BIT 3,B
    t = cpu.B & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_159(cpu): # 159 BIT 3,C
    t = cpu.C & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15A(cpu): # 15A BIT 3,D
    t = cpu.D & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15B(cpu): # 15B BIT 3,E
    t = cpu.E & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15C(cpu): # 15C BIT 3,H
    t = (cpu.HL >> 8) & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15D(cpu): # 15D BIT 3,L
    t = (cpu.HL & 0xFF) & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15E(cpu): # 15E BIT 3,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_15F(cpu): # 15F BIT 3,A
    t = cpu.A & (1 << 3)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_160(cpu): # 160 BIT 4,B
    t = cpu.B & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_161(cpu): # 161 BIT 4,C
    t = cpu.C & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_162(cpu): # 162 BIT 4,D
    t = cpu.D & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_163(cpu): # 163 BIT 4,E
    t = cpu.E & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_164(cpu): # 164 BIT 4,H
    t = (cpu.HL >> 8) & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_165(cpu): # 165 BIT 4,L
    t = (cpu.HL & 0xFF) & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_166(cpu): # 166 BIT 4,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_167(cpu): # 167 BIT 4,A
    t = cpu.A & (1 << 4)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_168(cpu): # 168 BIT 5,B
    t = cpu.B & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_169(cpu): # 169 BIT 5,C
    t = cpu.C & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16A(cpu): # 16A BIT 5,D
    t = cpu.D & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16B(cpu): # 16B BIT 5,E
    t = cpu.E & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16C(cpu): # 16C BIT 5,H
    t = (cpu.HL >> 8) & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16D(cpu): # 16D BIT 5,L
    t = (cpu.HL & 0xFF) & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16E(cpu): # 16E BIT 5,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_16F(cpu): # 16F BIT 5,A
    t = cpu.A & (1 << 5)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_170(cpu): # 170 BIT 6,B
    t = cpu.B & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_171(cpu): # 171 BIT 6,C
    t = cpu.C & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_172(cpu): # 172 BIT 6,D
    t = cpu.D & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_173(cpu): # 173 BIT 6,E
    t = cpu.E & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_174(cpu): # 174 BIT 6,H
    t = (cpu.HL >> 8) & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_175(cpu): # 175 BIT 6,L
    t = (cpu.HL & 0xFF) & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_176(cpu): # 176 BIT 6,(HL)
    cpu.cycles += 4
    t = cpu.mb.getitem(cpu.HL) & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_177(cpu): # 177 BIT 6,A
    t = cpu.A & (1 << 6)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_178(cpu): # 178 BIT 7,B
    t = cpu.B & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_179(cpu): # 179 BIT 7,C
    t = cpu.C & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_17A(cpu): # 17A BIT 7,D
    t = cpu.D & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_17B(cpu): # 17B BIT 7,E
    t = cpu.E & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


def BIT_17C(cpu): # 17C BIT 7,H
    t = (cpu.HL >> 8) & (1 << 7)
    flag = 0b00100000
    flag |= ((t & 0xFF) == 0) << FLAGZ
    cpu.F &= 0b00010000
    cpu.F |= flag
    cpu.PC += 2
    cpu.PC &= 0xFFFF
    cpu.cycles += 8


