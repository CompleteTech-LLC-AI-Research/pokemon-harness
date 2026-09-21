.notUsingSoftboiled2
	ld a, [wCurItem]
	cp SODA_POP
	ld b, 60 ; Soda Pop heal amount
	jr z, .addHealAmount
	ld b, 80 ; Lemonade heal amount
	jr nc, .addHealAmount
	cp FRESH_WATER
	ld b, 50 ; Fresh Water heal amount
	jr z, .addHealAmount
	cp SUPER_POTION
	ld b, 200 ; Hyper Potion heal amount
	jr c, .addHealAmount
	ld b, 50 ; Super Potion heal amount
	jr z, .addHealAmount
	ld b, 20 ; Potion heal amount
.addHealAmount
	pop de
	pop hl
	ld a, [hl]
	add b
	ld [hld], a
	ld [wHPBarNewHP], a
	ld a, [hl]
	ld [wHPBarNewHP+1], a
	jr nc, .noCarry
	inc [hl]
	ld a, [hl]
	ld [wHPBarNewHP + 1], a
.noCarry
	push de
	inc hl
	ld d, h
	ld e, l ; de now points to current HP
	ld hl, (MON_MAXHP + 1) - (MON_HP + 1)
	add hl, de ; hl now points to max HP
	ld a, [wCurItem]
	cp REVIVE
	jr z, .setCurrentHPToHalfMaxHP
	ld a, [hld]
	ld b, a
	ld a, [de]
	sub b
	dec de
	ld b, [hl]
	ld a, [de]
	sbc b
	jr nc, .setCurrentHPToMaxHp ; if current HP exceeds max HP after healing
	ld a, [wCurItem]
	cp HYPER_POTION
	jr c, .setCurrentHPToMaxHp ; if using a Full Restore or Max Potion
	cp MAX_REVIVE
	jr z, .setCurrentHPToMaxHp ; if using a Max Revive
	jr .updateInBattleData

.setCurrentHPToHalfMaxHP
	dec hl
	dec de
	ld a, [hli]
	srl a
	ld [de], a
	ld [wHPBarNewHP+1], a
	ld a, [hl]
	rr a
	inc de
	ld [de], a
	ld [wHPBarNewHP], a
	dec de
	jr .doneHealingPartyHP

.setCurrentHPToMaxHp
	ld a, [hli]
	ld [de], a
	ld [wHPBarNewHP+1], a
	inc de
	ld a, [hl]
	ld [de], a
	ld [wHPBarNewHP], a
	dec de
.doneHealingPartyHP ; done updating the pokemon's current HP in the party data structure
	ld a, [wCurItem]
	cp FULL_RESTORE
	jr nz, .updateInBattleData
	ld bc, MON_STATUS - (MON_MAXHP + 1)
	add hl, bc
	xor a
	ld [hl], a ; remove the status ailment in the party data
