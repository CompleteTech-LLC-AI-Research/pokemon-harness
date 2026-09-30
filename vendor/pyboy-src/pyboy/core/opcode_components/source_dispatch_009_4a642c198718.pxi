def execute_opcode(cpu, opcode, v):

    if opcode == 0x00:
        return NOP_00(cpu)
    elif opcode == 0x01:
        return LD_01(cpu, v)
    elif opcode == 0x02:
        return LD_02(cpu)
    elif opcode == 0x03:
        return INC_03(cpu)
    elif opcode == 0x04:
        return INC_04(cpu)
    elif opcode == 0x05:
        return DEC_05(cpu)
    elif opcode == 0x06:
        return LD_06(cpu, v)
    elif opcode == 0x07:
        return RLCA_07(cpu)
    elif opcode == 0x08:
        return LD_08(cpu, v)
    elif opcode == 0x09:
        return ADD_09(cpu)
    elif opcode == 0x0A:
        return LD_0A(cpu)
    elif opcode == 0x0B:
        return DEC_0B(cpu)
    elif opcode == 0x0C:
        return INC_0C(cpu)
    elif opcode == 0x0D:
        return DEC_0D(cpu)
    elif opcode == 0x0E:
        return LD_0E(cpu, v)
    elif opcode == 0x0F:
        return RRCA_0F(cpu)
    elif opcode == 0x10:
        return STOP_10(cpu, v)
    elif opcode == 0x11:
        return LD_11(cpu, v)
    elif opcode == 0x12:
        return LD_12(cpu)
    elif opcode == 0x13:
        return INC_13(cpu)
    elif opcode == 0x14:
        return INC_14(cpu)
    elif opcode == 0x15:
        return DEC_15(cpu)
    elif opcode == 0x16:
        return LD_16(cpu, v)
    elif opcode == 0x17:
        return RLA_17(cpu)
    elif opcode == 0x18:
        return JR_18(cpu, v)
    elif opcode == 0x19:
        return ADD_19(cpu)
    elif opcode == 0x1A:
        return LD_1A(cpu)
    elif opcode == 0x1B:
        return DEC_1B(cpu)
    elif opcode == 0x1C:
        return INC_1C(cpu)
    elif opcode == 0x1D:
        return DEC_1D(cpu)
    elif opcode == 0x1E:
        return LD_1E(cpu, v)
    elif opcode == 0x1F:
        return RRA_1F(cpu)
    elif opcode == 0x20:
        return JR_20(cpu, v)
    elif opcode == 0x21:
        return LD_21(cpu, v)
    elif opcode == 0x22:
        return LD_22(cpu)
    elif opcode == 0x23:
        return INC_23(cpu)
    elif opcode == 0x24:
        return INC_24(cpu)
    elif opcode == 0x25:
        return DEC_25(cpu)
    elif opcode == 0x26:
        return LD_26(cpu, v)
    elif opcode == 0x27:
        return DAA_27(cpu)
    elif opcode == 0x28:
        return JR_28(cpu, v)
    elif opcode == 0x29:
        return ADD_29(cpu)
    elif opcode == 0x2A:
        return LD_2A(cpu)
    elif opcode == 0x2B:
        return DEC_2B(cpu)
    elif opcode == 0x2C:
        return INC_2C(cpu)
    elif opcode == 0x2D:
        return DEC_2D(cpu)
    elif opcode == 0x2E:
        return LD_2E(cpu, v)
    elif opcode == 0x2F:
        return CPL_2F(cpu)
    elif opcode == 0x30:
        return JR_30(cpu, v)
    elif opcode == 0x31:
        return LD_31(cpu, v)
    elif opcode == 0x32:
        return LD_32(cpu)
    elif opcode == 0x33:
        return INC_33(cpu)
    elif opcode == 0x34:
        return INC_34(cpu)
    elif opcode == 0x35:
        return DEC_35(cpu)
    elif opcode == 0x36:
        return LD_36(cpu, v)
    elif opcode == 0x37:
        return SCF_37(cpu)
    elif opcode == 0x38:
        return JR_38(cpu, v)
    elif opcode == 0x39:
        return ADD_39(cpu)
    elif opcode == 0x3A:
        return LD_3A(cpu)
    elif opcode == 0x3B:
        return DEC_3B(cpu)
    elif opcode == 0x3C:
        return INC_3C(cpu)
    elif opcode == 0x3D:
        return DEC_3D(cpu)
    elif opcode == 0x3E:
        return LD_3E(cpu, v)
    elif opcode == 0x3F:
        return CCF_3F(cpu)
    elif opcode == 0x40:
        return LD_40(cpu)
    elif opcode == 0x41:
        return LD_41(cpu)
    elif opcode == 0x42:
        return LD_42(cpu)
    elif opcode == 0x43:
        return LD_43(cpu)
    elif opcode == 0x44:
        return LD_44(cpu)
    elif opcode == 0x45:
        return LD_45(cpu)
    elif opcode == 0x46:
        return LD_46(cpu)
    elif opcode == 0x47:
        return LD_47(cpu)
    elif opcode == 0x48:
        return LD_48(cpu)
    elif opcode == 0x49:
        return LD_49(cpu)
    elif opcode == 0x4A:
        return LD_4A(cpu)
    elif opcode == 0x4B:
        return LD_4B(cpu)
    elif opcode == 0x4C:
        return LD_4C(cpu)
    elif opcode == 0x4D:
        return LD_4D(cpu)
    elif opcode == 0x4E:
        return LD_4E(cpu)
    elif opcode == 0x4F:
        return LD_4F(cpu)
    elif opcode == 0x50:
        return LD_50(cpu)
    elif opcode == 0x51:
        return LD_51(cpu)
    elif opcode == 0x52:
        return LD_52(cpu)
    elif opcode == 0x53:
        return LD_53(cpu)
    elif opcode == 0x54:
        return LD_54(cpu)
    elif opcode == 0x55:
        return LD_55(cpu)
    elif opcode == 0x56:
        return LD_56(cpu)
    elif opcode == 0x57:
        return LD_57(cpu)
    elif opcode == 0x58:
        return LD_58(cpu)
    elif opcode == 0x59:
        return LD_59(cpu)
    elif opcode == 0x5A:
        return LD_5A(cpu)
    elif opcode == 0x5B:
        return LD_5B(cpu)
    elif opcode == 0x5C:
        return LD_5C(cpu)
    elif opcode == 0x5D:
        return LD_5D(cpu)
    elif opcode == 0x5E:
        return LD_5E(cpu)
    elif opcode == 0x5F:
        return LD_5F(cpu)
    elif opcode == 0x60:
        return LD_60(cpu)
    elif opcode == 0x61:
        return LD_61(cpu)
    elif opcode == 0x62:
        return LD_62(cpu)
    elif opcode == 0x63:
        return LD_63(cpu)
    elif opcode == 0x64:
        return LD_64(cpu)
    elif opcode == 0x65:
        return LD_65(cpu)
    elif opcode == 0x66:
        return LD_66(cpu)
    elif opcode == 0x67:
        return LD_67(cpu)
    elif opcode == 0x68:
        return LD_68(cpu)
    elif opcode == 0x69:
        return LD_69(cpu)
    elif opcode == 0x6A:
        return LD_6A(cpu)
    elif opcode == 0x6B:
        return LD_6B(cpu)
    elif opcode == 0x6C:
        return LD_6C(cpu)
    elif opcode == 0x6D:
        return LD_6D(cpu)
    elif opcode == 0x6E:
        return LD_6E(cpu)
    elif opcode == 0x6F:
        return LD_6F(cpu)
    elif opcode == 0x70:
        return LD_70(cpu)
    elif opcode == 0x71:
        return LD_71(cpu)
    elif opcode == 0x72:
        return LD_72(cpu)
    elif opcode == 0x73:
        return LD_73(cpu)
    elif opcode == 0x74:
        return LD_74(cpu)
    elif opcode == 0x75:
        return LD_75(cpu)
    elif opcode == 0x76:
        return HALT_76(cpu)
    elif opcode == 0x77:
        return LD_77(cpu)
    elif opcode == 0x78:
        return LD_78(cpu)
    elif opcode == 0x79:
        return LD_79(cpu)
    elif opcode == 0x7A:
        return LD_7A(cpu)
    elif opcode == 0x7B:
        return LD_7B(cpu)
    elif opcode == 0x7C:
        return LD_7C(cpu)
    elif opcode == 0x7D:
        return LD_7D(cpu)
    elif opcode == 0x7E:
        return LD_7E(cpu)
    elif opcode == 0x7F:
        return LD_7F(cpu)
    elif opcode == 0x80:
        return ADD_80(cpu)
    elif opcode == 0x81:
        return ADD_81(cpu)
    elif opcode == 0x82:
        return ADD_82(cpu)
    elif opcode == 0x83:
        return ADD_83(cpu)
    elif opcode == 0x84:
        return ADD_84(cpu)
    elif opcode == 0x85:
        return ADD_85(cpu)
    elif opcode == 0x86:
        return ADD_86(cpu)
    elif opcode == 0x87:
        return ADD_87(cpu)
    elif opcode == 0x88:
        return ADC_88(cpu)
    elif opcode == 0x89:
        return ADC_89(cpu)
    elif opcode == 0x8A:
        return ADC_8A(cpu)
    elif opcode == 0x8B:
        return ADC_8B(cpu)
    elif opcode == 0x8C:
        return ADC_8C(cpu)
    elif opcode == 0x8D:
        return ADC_8D(cpu)
    elif opcode == 0x8E:
        return ADC_8E(cpu)
    elif opcode == 0x8F:
        return ADC_8F(cpu)
    elif opcode == 0x90:
        return SUB_90(cpu)
    elif opcode == 0x91:
        return SUB_91(cpu)
    elif opcode == 0x92:
        return SUB_92(cpu)
    elif opcode == 0x93:
        return SUB_93(cpu)
    elif opcode == 0x94:
        return SUB_94(cpu)
    elif opcode == 0x95:
        return SUB_95(cpu)
    elif opcode == 0x96:
        return SUB_96(cpu)
    elif opcode == 0x97:
        return SUB_97(cpu)
    elif opcode == 0x98:
        return SBC_98(cpu)
    elif opcode == 0x99:
        return SBC_99(cpu)
    elif opcode == 0x9A:
        return SBC_9A(cpu)
    elif opcode == 0x9B:
        return SBC_9B(cpu)
    elif opcode == 0x9C:
        return SBC_9C(cpu)
    elif opcode == 0x9D:
        return SBC_9D(cpu)
    elif opcode == 0x9E:
        return SBC_9E(cpu)
    elif opcode == 0x9F:
        return SBC_9F(cpu)
    elif opcode == 0xA0:
        return AND_A0(cpu)
    elif opcode == 0xA1:
        return AND_A1(cpu)
    elif opcode == 0xA2:
        return AND_A2(cpu)
    elif opcode == 0xA3:
        return AND_A3(cpu)
    elif opcode == 0xA4:
        return AND_A4(cpu)
    elif opcode == 0xA5:
        return AND_A5(cpu)
    elif opcode == 0xA6:
        return AND_A6(cpu)
    elif opcode == 0xA7:
        return AND_A7(cpu)
    elif opcode == 0xA8:
        return XOR_A8(cpu)
    elif opcode == 0xA9:
        return XOR_A9(cpu)
    elif opcode == 0xAA:
        return XOR_AA(cpu)
    elif opcode == 0xAB:
        return XOR_AB(cpu)
    elif opcode == 0xAC:
        return XOR_AC(cpu)
    elif opcode == 0xAD:
        return XOR_AD(cpu)
    elif opcode == 0xAE:
        return XOR_AE(cpu)
    elif opcode == 0xAF:
        return XOR_AF(cpu)
    elif opcode == 0xB0:
        return OR_B0(cpu)
    elif opcode == 0xB1:
        return OR_B1(cpu)
    elif opcode == 0xB2:
        return OR_B2(cpu)
    elif opcode == 0xB3:
        return OR_B3(cpu)
    elif opcode == 0xB4:
        return OR_B4(cpu)
    elif opcode == 0xB5:
        return OR_B5(cpu)
    elif opcode == 0xB6:
        return OR_B6(cpu)
    elif opcode == 0xB7:
        return OR_B7(cpu)
    elif opcode == 0xB8:
        return CP_B8(cpu)
    elif opcode == 0xB9:
        return CP_B9(cpu)
    elif opcode == 0xBA:
        return CP_BA(cpu)
    elif opcode == 0xBB:
        return CP_BB(cpu)
    elif opcode == 0xBC:
        return CP_BC(cpu)
    elif opcode == 0xBD:
        return CP_BD(cpu)
    elif opcode == 0xBE:
        return CP_BE(cpu)
    elif opcode == 0xBF:
        return CP_BF(cpu)
    elif opcode == 0xC0:
        return RET_C0(cpu)
    elif opcode == 0xC1:
        return POP_C1(cpu)
    elif opcode == 0xC2:
        return JP_C2(cpu, v)
    elif opcode == 0xC3:
        return JP_C3(cpu, v)
    elif opcode == 0xC4:
        return CALL_C4(cpu, v)
    elif opcode == 0xC5:
        return PUSH_C5(cpu)
    elif opcode == 0xC6:
        return ADD_C6(cpu, v)
    elif opcode == 0xC7:
        return RST_C7(cpu)
    elif opcode == 0xC8:
        return RET_C8(cpu)
    elif opcode == 0xC9:
        return RET_C9(cpu)
    elif opcode == 0xCA:
        return JP_CA(cpu, v)
    elif opcode == 0xCB:
        return PREFIX_CB(cpu)
    elif opcode == 0xCC:
        return CALL_CC(cpu, v)
    elif opcode == 0xCD:
        return CALL_CD(cpu, v)
    elif opcode == 0xCE:
        return ADC_CE(cpu, v)
    elif opcode == 0xCF:
        return RST_CF(cpu)
    elif opcode == 0xD0:
        return RET_D0(cpu)
    elif opcode == 0xD1:
        return POP_D1(cpu)
    elif opcode == 0xD2:
        return JP_D2(cpu, v)
    elif opcode == 0xD3:
        return no_opcode(cpu)
    elif opcode == 0xD4:
        return CALL_D4(cpu, v)
    elif opcode == 0xD5:
        return PUSH_D5(cpu)
    elif opcode == 0xD6:
        return SUB_D6(cpu, v)
    elif opcode == 0xD7:
        return RST_D7(cpu)
    elif opcode == 0xD8:
        return RET_D8(cpu)
    elif opcode == 0xD9:
        return RETI_D9(cpu)
    elif opcode == 0xDA:
        return JP_DA(cpu, v)
    elif opcode == 0xDB:
        return BRK(cpu)
    elif opcode == 0xDC:
        return CALL_DC(cpu, v)
    elif opcode == 0xDD:
        return no_opcode(cpu)
    elif opcode == 0xDE:
        return SBC_DE(cpu, v)
    elif opcode == 0xDF:
        return RST_DF(cpu)
    elif opcode == 0xE0:
        return LDH_E0(cpu, v)
    elif opcode == 0xE1:
        return POP_E1(cpu)
    elif opcode == 0xE2:
        return LD_E2(cpu)
    elif opcode == 0xE3:
        return no_opcode(cpu)
    elif opcode == 0xE4:
        return no_opcode(cpu)
    elif opcode == 0xE5:
        return PUSH_E5(cpu)
    elif opcode == 0xE6:
        return AND_E6(cpu, v)
    elif opcode == 0xE7:
        return RST_E7(cpu)
    elif opcode == 0xE8:
        return ADD_E8(cpu, v)
    elif opcode == 0xE9:
        return JP_E9(cpu)
    elif opcode == 0xEA:
        return LD_EA(cpu, v)
    elif opcode == 0xEB:
        return no_opcode(cpu)
    elif opcode == 0xEC:
        return no_opcode(cpu)
    elif opcode == 0xED:
        return no_opcode(cpu)
    elif opcode == 0xEE:
        return XOR_EE(cpu, v)
    elif opcode == 0xEF:
        return RST_EF(cpu)
    elif opcode == 0xF0:
        return LDH_F0(cpu, v)
    elif opcode == 0xF1:
        return POP_F1(cpu)
    elif opcode == 0xF2:
        return LD_F2(cpu)
    elif opcode == 0xF3:
        return DI_F3(cpu)
    elif opcode == 0xF4:
        return no_opcode(cpu)
    elif opcode == 0xF5:
        return PUSH_F5(cpu)
    elif opcode == 0xF6:
        return OR_F6(cpu, v)
    elif opcode == 0xF7:
        return RST_F7(cpu)
    elif opcode == 0xF8:
        return LD_F8(cpu, v)
    elif opcode == 0xF9:
        return LD_F9(cpu)
    elif opcode == 0xFA:
        return LD_FA(cpu, v)
    elif opcode == 0xFB:
        return EI_FB(cpu)
    elif opcode == 0xFC:
        return no_opcode(cpu)
    elif opcode == 0xFD:
        return no_opcode(cpu)
    elif opcode == 0xFE:
        return CP_FE(cpu, v)
    elif opcode == 0xFF:
        return RST_FF(cpu)
    elif opcode == 0x100:
        return RLC_100(cpu)
    elif opcode == 0x101:
        return RLC_101(cpu)
    elif opcode == 0x102:
        return RLC_102(cpu)
    elif opcode == 0x103:
        return RLC_103(cpu)
    elif opcode == 0x104:
        return RLC_104(cpu)
    elif opcode == 0x105:
        return RLC_105(cpu)
    elif opcode == 0x106:
        return RLC_106(cpu)
    elif opcode == 0x107:
        return RLC_107(cpu)
    elif opcode == 0x108:
        return RRC_108(cpu)
    elif opcode == 0x109:
        return RRC_109(cpu)
    elif opcode == 0x10A:
        return RRC_10A(cpu)
    elif opcode == 0x10B:
        return RRC_10B(cpu)
    elif opcode == 0x10C:
        return RRC_10C(cpu)
    elif opcode == 0x10D:
        return RRC_10D(cpu)
    elif opcode == 0x10E:
        return RRC_10E(cpu)
    elif opcode == 0x10F:
        return RRC_10F(cpu)
    elif opcode == 0x110:
        return RL_110(cpu)
    elif opcode == 0x111:
        return RL_111(cpu)
    elif opcode == 0x112:
        return RL_112(cpu)
    elif opcode == 0x113:
        return RL_113(cpu)
    elif opcode == 0x114:
        return RL_114(cpu)
    elif opcode == 0x115:
        return RL_115(cpu)
    elif opcode == 0x116:
        return RL_116(cpu)
    elif opcode == 0x117:
        return RL_117(cpu)
    elif opcode == 0x118:
        return RR_118(cpu)
    elif opcode == 0x119:
        return RR_119(cpu)
    elif opcode == 0x11A:
        return RR_11A(cpu)
    elif opcode == 0x11B:
        return RR_11B(cpu)
    elif opcode == 0x11C:
        return RR_11C(cpu)
    elif opcode == 0x11D:
        return RR_11D(cpu)
    elif opcode == 0x11E:
        return RR_11E(cpu)
    elif opcode == 0x11F:
        return RR_11F(cpu)
    elif opcode == 0x120:
        return SLA_120(cpu)
    elif opcode == 0x121:
        return SLA_121(cpu)
    elif opcode == 0x122:
        return SLA_122(cpu)
    elif opcode == 0x123:
        return SLA_123(cpu)
    elif opcode == 0x124:
        return SLA_124(cpu)
    elif opcode == 0x125:
        return SLA_125(cpu)
    elif opcode == 0x126:
        return SLA_126(cpu)
    elif opcode == 0x127:
        return SLA_127(cpu)
    elif opcode == 0x128:
        return SRA_128(cpu)
    elif opcode == 0x129:
        return SRA_129(cpu)
    elif opcode == 0x12A:
        return SRA_12A(cpu)
    elif opcode == 0x12B:
        return SRA_12B(cpu)
    elif opcode == 0x12C:
        return SRA_12C(cpu)
    elif opcode == 0x12D:
        return SRA_12D(cpu)
    elif opcode == 0x12E:
        return SRA_12E(cpu)
    elif opcode == 0x12F:
        return SRA_12F(cpu)
    elif opcode == 0x130:
        return SWAP_130(cpu)
    elif opcode == 0x131:
        return SWAP_131(cpu)
    elif opcode == 0x132:
        return SWAP_132(cpu)
    elif opcode == 0x133:
        return SWAP_133(cpu)
    elif opcode == 0x134:
        return SWAP_134(cpu)
    elif opcode == 0x135:
        return SWAP_135(cpu)
    elif opcode == 0x136:
        return SWAP_136(cpu)
    elif opcode == 0x137:
        return SWAP_137(cpu)
    elif opcode == 0x138:
        return SRL_138(cpu)
    elif opcode == 0x139:
        return SRL_139(cpu)
    elif opcode == 0x13A:
        return SRL_13A(cpu)
    elif opcode == 0x13B:
        return SRL_13B(cpu)
    elif opcode == 0x13C:
        return SRL_13C(cpu)
    elif opcode == 0x13D:
        return SRL_13D(cpu)
    elif opcode == 0x13E:
        return SRL_13E(cpu)
    elif opcode == 0x13F:
        return SRL_13F(cpu)
    elif opcode == 0x140:
        return BIT_140(cpu)
    elif opcode == 0x141:
        return BIT_141(cpu)
    elif opcode == 0x142:
        return BIT_142(cpu)
    elif opcode == 0x143:
        return BIT_143(cpu)
    elif opcode == 0x144:
        return BIT_144(cpu)
    elif opcode == 0x145:
        return BIT_145(cpu)
    elif opcode == 0x146:
        return BIT_146(cpu)
    elif opcode == 0x147:
        return BIT_147(cpu)
    elif opcode == 0x148:
        return BIT_148(cpu)
    elif opcode == 0x149:
        return BIT_149(cpu)
    elif opcode == 0x14A:
        return BIT_14A(cpu)
    elif opcode == 0x14B:
        return BIT_14B(cpu)
    elif opcode == 0x14C:
        return BIT_14C(cpu)
    elif opcode == 0x14D:
        return BIT_14D(cpu)
    elif opcode == 0x14E:
        return BIT_14E(cpu)
    elif opcode == 0x14F:
        return BIT_14F(cpu)
    elif opcode == 0x150:
        return BIT_150(cpu)
    elif opcode == 0x151:
        return BIT_151(cpu)
    elif opcode == 0x152:
        return BIT_152(cpu)
    elif opcode == 0x153:
        return BIT_153(cpu)
    elif opcode == 0x154:
        return BIT_154(cpu)
    elif opcode == 0x155:
        return BIT_155(cpu)
    elif opcode == 0x156:
        return BIT_156(cpu)
    elif opcode == 0x157:
        return BIT_157(cpu)
    elif opcode == 0x158:
        return BIT_158(cpu)
    elif opcode == 0x159:
        return BIT_159(cpu)
    elif opcode == 0x15A:
        return BIT_15A(cpu)
    elif opcode == 0x15B:
        return BIT_15B(cpu)
    elif opcode == 0x15C:
        return BIT_15C(cpu)
    elif opcode == 0x15D:
        return BIT_15D(cpu)
    elif opcode == 0x15E:
        return BIT_15E(cpu)
    elif opcode == 0x15F:
        return BIT_15F(cpu)
    elif opcode == 0x160:
        return BIT_160(cpu)
    elif opcode == 0x161:
        return BIT_161(cpu)
    elif opcode == 0x162:
        return BIT_162(cpu)
    elif opcode == 0x163:
        return BIT_163(cpu)
    elif opcode == 0x164:
        return BIT_164(cpu)
    elif opcode == 0x165:
        return BIT_165(cpu)
    elif opcode == 0x166:
        return BIT_166(cpu)
    elif opcode == 0x167:
        return BIT_167(cpu)
    elif opcode == 0x168:
        return BIT_168(cpu)
    elif opcode == 0x169:
        return BIT_169(cpu)
    elif opcode == 0x16A:
        return BIT_16A(cpu)
    elif opcode == 0x16B:
        return BIT_16B(cpu)
    elif opcode == 0x16C:
        return BIT_16C(cpu)
    elif opcode == 0x16D:
        return BIT_16D(cpu)
    elif opcode == 0x16E:
        return BIT_16E(cpu)
    elif opcode == 0x16F:
        return BIT_16F(cpu)
    elif opcode == 0x170:
        return BIT_170(cpu)
    elif opcode == 0x171:
        return BIT_171(cpu)
    elif opcode == 0x172:
        return BIT_172(cpu)
    elif opcode == 0x173:
        return BIT_173(cpu)
    elif opcode == 0x174:
        return BIT_174(cpu)
    elif opcode == 0x175:
        return BIT_175(cpu)
    elif opcode == 0x176:
        return BIT_176(cpu)
    elif opcode == 0x177:
        return BIT_177(cpu)
    elif opcode == 0x178:
        return BIT_178(cpu)
    elif opcode == 0x179:
        return BIT_179(cpu)
    elif opcode == 0x17A:
        return BIT_17A(cpu)
    elif opcode == 0x17B:
        return BIT_17B(cpu)
    elif opcode == 0x17C:
        return BIT_17C(cpu)
    elif opcode == 0x17D:
        return BIT_17D(cpu)
    elif opcode == 0x17E:
        return BIT_17E(cpu)
    elif opcode == 0x17F:
        return BIT_17F(cpu)
    elif opcode == 0x180:
        return RES_180(cpu)
    elif opcode == 0x181:
        return RES_181(cpu)
    elif opcode == 0x182:
        return RES_182(cpu)
    elif opcode == 0x183:
        return RES_183(cpu)
    elif opcode == 0x184:
        return RES_184(cpu)
    elif opcode == 0x185:
        return RES_185(cpu)
    elif opcode == 0x186:
        return RES_186(cpu)
    elif opcode == 0x187:
        return RES_187(cpu)
    elif opcode == 0x188:
        return RES_188(cpu)
    elif opcode == 0x189:
        return RES_189(cpu)
    elif opcode == 0x18A:
        return RES_18A(cpu)
    elif opcode == 0x18B:
        return RES_18B(cpu)
    elif opcode == 0x18C:
        return RES_18C(cpu)
    elif opcode == 0x18D:
        return RES_18D(cpu)
    elif opcode == 0x18E:
        return RES_18E(cpu)
    elif opcode == 0x18F:
        return RES_18F(cpu)
    elif opcode == 0x190:
        return RES_190(cpu)
    elif opcode == 0x191:
        return RES_191(cpu)
    elif opcode == 0x192:
        return RES_192(cpu)
    elif opcode == 0x193:
        return RES_193(cpu)
    elif opcode == 0x194:
        return RES_194(cpu)
    elif opcode == 0x195:
        return RES_195(cpu)
    elif opcode == 0x196:
        return RES_196(cpu)
    elif opcode == 0x197:
        return RES_197(cpu)
    elif opcode == 0x198:
        return RES_198(cpu)
    elif opcode == 0x199:
        return RES_199(cpu)
    elif opcode == 0x19A:
        return RES_19A(cpu)
    elif opcode == 0x19B:
        return RES_19B(cpu)
    elif opcode == 0x19C:
        return RES_19C(cpu)
    elif opcode == 0x19D:
        return RES_19D(cpu)
    elif opcode == 0x19E:
        return RES_19E(cpu)
    elif opcode == 0x19F:
        return RES_19F(cpu)
    elif opcode == 0x1A0:
        return RES_1A0(cpu)
    elif opcode == 0x1A1:
        return RES_1A1(cpu)
    elif opcode == 0x1A2:
        return RES_1A2(cpu)
    elif opcode == 0x1A3:
        return RES_1A3(cpu)
    elif opcode == 0x1A4:
        return RES_1A4(cpu)
    elif opcode == 0x1A5:
        return RES_1A5(cpu)
    elif opcode == 0x1A6:
        return RES_1A6(cpu)
    elif opcode == 0x1A7:
        return RES_1A7(cpu)
    elif opcode == 0x1A8:
        return RES_1A8(cpu)
    elif opcode == 0x1A9:
        return RES_1A9(cpu)
    elif opcode == 0x1AA:
        return RES_1AA(cpu)
    elif opcode == 0x1AB:
        return RES_1AB(cpu)
    elif opcode == 0x1AC:
        return RES_1AC(cpu)
    elif opcode == 0x1AD:
        return RES_1AD(cpu)
    elif opcode == 0x1AE:
        return RES_1AE(cpu)
    elif opcode == 0x1AF:
        return RES_1AF(cpu)
    elif opcode == 0x1B0:
        return RES_1B0(cpu)
    elif opcode == 0x1B1:
        return RES_1B1(cpu)
    elif opcode == 0x1B2:
        return RES_1B2(cpu)
    elif opcode == 0x1B3:
        return RES_1B3(cpu)
    elif opcode == 0x1B4:
        return RES_1B4(cpu)
    elif opcode == 0x1B5:
        return RES_1B5(cpu)
    elif opcode == 0x1B6:
        return RES_1B6(cpu)
    elif opcode == 0x1B7:
        return RES_1B7(cpu)
    elif opcode == 0x1B8:
        return RES_1B8(cpu)
    elif opcode == 0x1B9:
        return RES_1B9(cpu)
    elif opcode == 0x1BA:
        return RES_1BA(cpu)
    elif opcode == 0x1BB:
        return RES_1BB(cpu)
    elif opcode == 0x1BC:
        return RES_1BC(cpu)
    elif opcode == 0x1BD:
        return RES_1BD(cpu)
    elif opcode == 0x1BE:
        return RES_1BE(cpu)
    elif opcode == 0x1BF:
        return RES_1BF(cpu)
    elif opcode == 0x1C0:
        return SET_1C0(cpu)
