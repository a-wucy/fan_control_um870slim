/* SPDX-License-Identifier: GPL-2.0 */
/* System-fan transactions, shared verbatim by the module and mock-EC tests.
 * Register map/sequences: FanControl.MinisforumUM d61e46c, F7bsdProfile.cs
 * and PawnIoF7bsdBackend.cs; see also mordilloSan/fan_control 5d0845a7,
 * module/minisforum_ec.c. Modified 2026-11-01..06; provenance in NOTICE.md.
 * No CPU table, CPU override, or direct PWM write is permitted here.
 */
#ifndef SYSTEM_FAN_CORE_H
#define SYSTEM_FAN_CORE_H

#ifdef __KERNEL__
#include <linux/errno.h>
#include <linux/string.h>
#include <linux/types.h>
#else
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
#endif

#define SF_TARGET 0x0885
#define SF_EFFECTIVE 0x0889
#define SF_OVERRIDE 0x088b
#define SF_MAX_CODE 51

struct sf_io {
	void *ctx;
	bool enabled;
	bool touched; /* A control write may have reached the EC. */
	int (*read)(void *ctx, unsigned short addr, unsigned char *value);
	int (*write)(void *ctx, unsigned short addr, unsigned char value);
	int (*identity)(void *ctx);
	void (*pause)(void *ctx, unsigned int ms);
};

struct sf_regs {
	unsigned char effective, override, target;
};

struct sf_expect {
	unsigned short addr;
	unsigned char value;
};

enum sf_state { SF_FIRMWARE, SF_FIXED, SF_RELEASING, SF_UNSUPPORTED };

static inline bool sf_plausible(unsigned char temp)
{
	return temp >= 1 && temp <= 120;
}

/* This is the entire write allowlist. No caller can opt out of it. */
static inline bool sf_can_write(unsigned short addr, unsigned char value)
{
	return (addr == SF_TARGET && value <= SF_MAX_CODE) ||
	       (addr == SF_OVERRIDE && (value == 0 || value == 0xff));
}

static inline bool sf_can_read(unsigned short addr)
{
	return addr == 0x0305 || (addr >= 0x0330 && addr <= 0x0338) ||
	       addr == SF_TARGET || addr == SF_EFFECTIVE || addr == SF_OVERRIDE ||
	       (addr >= 0x08c0 && addr <= 0x08c2) || addr == 0x1804 ||
	       addr == 0x180c || addr == 0x1820 || addr == 0x1821 ||
	       addr == 0x1841 || (addr >= 0x2000 && addr <= 0x2002) || addr == 0x200d;
}

static inline enum sf_state sf_classify(const struct sf_regs *r)
{
	if (r->target > SF_MAX_CODE)
		return SF_UNSUPPORTED;
	if (!r->override) {
		if (sf_plausible(r->effective))
			return SF_FIRMWARE;
		return r->effective == 0xff ? SF_RELEASING : SF_UNSUPPORTED;
	}
	if (r->override == 0xff &&
	    (r->effective == 0xff || sf_plausible(r->effective)))
		return SF_FIXED;
	return SF_UNSUPPORTED;
}

static inline int sf_read_regs(struct sf_io *io, struct sf_regs *r)
{
	int ret;

	ret = io->read(io->ctx, SF_EFFECTIVE, &r->effective);
	if (!ret)
		ret = io->read(io->ctx, SF_OVERRIDE, &r->override);
	if (!ret)
		ret = io->read(io->ctx, SF_TARGET, &r->target);
	return ret;
}

static inline int sf_policy(struct sf_io *io)
{
	static const unsigned short addresses[] = {
		0x0330, 0x0331, 0x0332, 0x0333, 0x0334, 0x0335,
		0x0336, 0x0337, 0x0338, 0x08c0, 0x08c1, 0x08c2,
	};
	static const unsigned char expected[] = { 0, 25, 0, 15, 83, 25, 40, 100, 83, 0, 0, 0 };
	unsigned char previous[sizeof(expected)], observed[sizeof(expected)];
	unsigned int attempt, i;
	int ret;

	for (attempt = 0; attempt < 4; attempt++) {
		for (i = 0; i < sizeof(expected); i++) {
			ret = io->read(io->ctx, addresses[i], &observed[i]);
			if (ret)
				return ret;
		}
		if (attempt && !memcmp(previous, observed, sizeof(observed)))
			return memcmp(observed, expected, sizeof(expected)) ? -ENODEV : 0;
		memcpy(previous, observed, sizeof(observed));
	}
	return -EIO;
}

static inline int sf_write(struct sf_io *io, const struct sf_expect *before,
			   size_t count, unsigned short addr, unsigned char value)
{
	unsigned char observed;
	size_t i;
	int ret;

	/* Reject before any hardware access, including in observation mode. */
	if (!io->enabled || !sf_can_write(addr, value))
		return -EPERM;
	ret = io->identity(io->ctx);
	if (!ret)
		ret = sf_policy(io);
	if (ret)
		return ret;
	for (i = 0; i < count; i++) {
		ret = io->read(io->ctx, before[i].addr, &observed);
		if (ret)
			return ret;
		if (observed != before[i].value)
			return -EIO;
	}
	io->touched = true;
	ret = io->write(io->ctx, addr, value);
	if (!ret)
		ret = io->read(io->ctx, addr, &observed);
	if (ret)
		return ret;
	return observed == value ? 0 : -EIO;
}

static inline int sf_wait(struct sf_io *io, bool fixed)
{
	unsigned char value;
	unsigned int i;
	int ret;

	for (i = 0; i < 16; i++) {
		ret = io->read(io->ctx, SF_EFFECTIVE, &value);
		if (ret)
			return ret;
		if (fixed ? value == 0xff : sf_plausible(value))
			return 0;
		if (i != 15)
			io->pause(io->ctx, 100);
	}
	return -EIO;
}

/* Explicit recovery also accepts a recognized leftover fixed-target state.
 * Policy tables themselves are NEVER modified: restore seeds the maximum,
 * clears the override, then verifies firmware ownership.
 */
static inline int sf_release(struct sf_io *io)
{
	struct sf_regs r;
	struct sf_expect before[2];
	unsigned int step;
	int ret;

	/* "Firmware-owned" is not enough if the original policy no longer
	 * matches. Never report a successful rollback on an unknown controller
	 * or a changed policy, even when no repair write would be needed.
	 */
	ret = io->identity(io->ctx);
	if (!ret)
		ret = sf_policy(io);
	if (ret)
		return ret;
	for (step = 0; step < 4; step++) {
		ret = sf_read_regs(io, &r);
		if (ret)
			return ret;
		switch (sf_classify(&r)) {
		case SF_FIRMWARE:
			io->touched = false;
			return 0;
		case SF_RELEASING:
			ret = sf_wait(io, false);
			if (ret)
				return ret;
			break;
		case SF_FIXED:
			before[0] = (struct sf_expect){ SF_OVERRIDE, r.override };
			before[1] = (struct sf_expect){ SF_TARGET, r.target };
			if (r.target != SF_MAX_CODE)
				ret = sf_write(io, before, 2, SF_TARGET, SF_MAX_CODE);
			else
				ret = sf_write(io, before, 2, SF_OVERRIDE, 0);
			if (ret)
				return ret;
			break;
		default:
			return -EIO;
		}
	}
	return -EIO;
}

static inline int sf_engage(struct sf_io *io, int *start)
{
	struct sf_expect before[3];
	struct sf_regs r;
	int attempt, ret;

	for (attempt = 0; attempt < 2; attempt++) {
		ret = sf_read_regs(io, &r);
		if (ret)
			return ret;
		if (sf_classify(&r) != SF_FIRMWARE)
			return -EBUSY;
		before[0] = (struct sf_expect){ SF_EFFECTIVE, r.effective };
		before[1] = (struct sf_expect){ SF_OVERRIDE, r.override };
		before[2] = (struct sf_expect){ SF_TARGET, r.target };
		ret = sf_write(io, before, 3, SF_OVERRIDE, 0xff);
		if (!ret || io->touched)
			break;
	}
	if (!ret)
		ret = sf_wait(io, true);
	if (!ret)
		ret = sf_read_regs(io, &r);
	if (ret)
		return ret;
	if (r.override != 0xff || r.effective != 0xff || r.target > SF_MAX_CODE)
		return -EIO;
	*start = r.target;
	return 0;
}

static inline int sf_set(struct sf_io *io, int applied, unsigned char code)
{
	struct sf_expect before[] = {
		{ SF_EFFECTIVE, 0xff }, { SF_OVERRIDE, 0xff }, { SF_TARGET, applied },
	};

	if (!io->touched)
		return -EBUSY;
	return sf_write(io, before, 3, SF_TARGET, code);
}

static inline int sf_next_code(int applied, int goal, int minimum)
{
	int next;

	if (applied == goal)
		return applied;
	if (!applied)
		return 12; /* Assist startup at 1200 nominal RPM, then ramp to goal. */
	next = applied + (goal > applied ? 1 : -1);
	if (next > 0 && next < minimum)
		next = goal > applied ? minimum : 0;
	return next;
}

#endif
