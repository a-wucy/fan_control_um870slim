/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stdio.h>
#include "../module/system_fan_core.h"

struct mock {
	unsigned char ram[65536];
	unsigned int writes, pauses;
	bool bad_identity, no_handoff, no_release, drop_write, fail_write;
};

static int read_byte(void *ctx, unsigned short addr, unsigned char *value)
{
	struct mock *m = ctx;

	assert(sf_can_read(addr));
	*value = m->ram[addr];
	return 0;
}

static int write_byte(void *ctx, unsigned short addr, unsigned char value)
{
	struct mock *m = ctx;

	assert(sf_can_write(addr, value));
	m->writes++;
	if (m->fail_write)
		return -EIO;
	if (m->drop_write)
		return 0;
	m->ram[addr] = value;
	if (addr == SF_OVERRIDE) {
		if (value == 0xff && !m->no_handoff)
			m->ram[SF_EFFECTIVE] = 0xff;
		if (!value && !m->no_release) {
			m->ram[SF_EFFECTIVE] = 40;
			m->ram[SF_TARGET] = 15; /* Normal firmware takes its target back. */
		}
	}
	return 0;
}

static int identity(void *ctx)
{
	return ((struct mock *)ctx)->bad_identity ? -ENODEV : 0;
}

static void pause_mock(void *ctx, unsigned int ms)
{
	assert(ms == 100);
	((struct mock *)ctx)->pauses++;
}

static struct sf_io setup(struct mock *m, bool enabled)
{
	static const unsigned char policy[] = { 0, 25, 0, 15, 83, 25, 40, 100, 83 };

	memset(m, 0, sizeof(*m));
	memset(m->ram, 0x5a, sizeof(m->ram)); /* Detect any unrelated memory change. */
	memcpy(m->ram + 0x0330, policy, sizeof(policy));
	memset(m->ram + 0x08c0, 0, 3);
	m->ram[SF_TARGET] = 15;
	m->ram[SF_EFFECTIVE] = 40;
	m->ram[SF_OVERRIDE] = 0;
	return (struct sf_io){
		.ctx = m, .enabled = enabled, .read = read_byte, .write = write_byte,
		.identity = identity, .pause = pause_mock,
	};
}

static void allowlists(void)
{
	unsigned int addr, value;

	/* Exhaust the ENTIRE 16-bit EC address / 8-bit value space. */
	for (addr = 0; addr <= 0xffff; addr++) {
		for (value = 0; value <= 0xff; value++) {
			bool expected = (addr == 0x0885 && value <= 51) ||
				(addr == 0x088b && (value == 0 || value == 255));
			assert(sf_can_write(addr, value) == expected);
		}
	}
	assert(!sf_can_read(0x0309)); /* No CPU telemetry either. */
	assert(!sf_can_read(0x088a));
	assert(!sf_can_read(0x0310));
	assert(!sf_can_read(0x08b0));
}

static void observation(void)
{
	struct mock m;
	struct sf_io io = setup(&m, false);
	int start;

	assert(sf_policy(&io) == 0);
	assert(sf_release(&io) == 0);
	assert(sf_engage(&io, &start) == -EPERM);
	assert(sf_write(&io, NULL, 0, SF_TARGET, 0) == -EPERM);
	m.ram[SF_OVERRIDE] = m.ram[SF_EFFECTIVE] = 0xff;
	assert(sf_release(&io) == -EPERM); /* Even leftover repair is blocked. */
	assert(!io.touched && m.writes == 0);
}

static void normal_control(void)
{
	struct mock m;
	struct sf_io io = setup(&m, true);
	unsigned char original[65536];
	int current;

	memcpy(original, m.ram, sizeof(original));
	assert(sf_engage(&io, &current) == 0 && current == 15 && io.touched);
	assert(sf_set(&io, current, 0) == 0);
	assert(sf_set(&io, 0, 12) == 0);
	assert(sf_set(&io, 12, 8) == 0);
	assert(sf_release(&io) == 0);
	assert(!io.touched && m.ram[SF_OVERRIDE] == 0);
	assert(memcmp(original, m.ram, sizeof(original)) == 0);
	assert(m.writes == 6); /* Override, three targets, maximum, clear override. */
}

static void guards(void)
{
	struct mock m;
	struct sf_io io;
	struct sf_expect wrong = { SF_TARGET, 99 };
	int start;

	io = setup(&m, true);
	assert(sf_write(&io, NULL, 0, 0x0310, 0) == -EPERM);
	assert(sf_write(&io, NULL, 0, SF_TARGET, 52) == -EPERM);
	assert(sf_write(&io, NULL, 0, SF_OVERRIDE, 1) == -EPERM);
	assert(sf_write(&io, &wrong, 1, SF_TARGET, 0) == -EIO);
	assert(m.writes == 0 && !io.touched);
	m.bad_identity = true;
	assert(sf_engage(&io, &start) == -ENODEV && !m.writes);
	assert(sf_release(&io) == -ENODEV && !m.writes);
	io = setup(&m, true);
	m.ram[0x0333]++;
	assert(sf_engage(&io, &start) == -ENODEV && !m.writes);
	assert(sf_release(&io) == -ENODEV && !m.writes);
	io = setup(&m, true);
	assert(sf_set(&io, 15, 0) == -EBUSY && !m.writes);
	assert(sf_engage(&io, &start) == 0);
	m.ram[SF_TARGET] = 14; /* Unexpected concurrent writer. */
	assert(sf_set(&io, 15, 0) == -EIO && m.writes == 1);
	assert(sf_release(&io) == 0);
}

static void failures_and_recovery(void)
{
	struct mock m;
	struct sf_io io;
	int start;

	io = setup(&m, true);
	m.drop_write = true;
	assert(sf_engage(&io, &start) == -EIO && io.touched);
	m.drop_write = false;
	assert(sf_release(&io) == 0 && !io.touched);
	io = setup(&m, true);
	m.fail_write = true;
	assert(sf_engage(&io, &start) == -EIO && io.touched);
	m.fail_write = false;
	assert(sf_release(&io) == 0 && !io.touched);
	io = setup(&m, true);
	m.no_handoff = true;
	assert(sf_engage(&io, &start) == -EIO && io.touched && m.pauses == 15);
	assert(sf_release(&io) == 0);
	io = setup(&m, true);
	assert(sf_engage(&io, &start) == 0);
	m.no_release = true;
	assert(sf_release(&io) == -EIO && io.touched);
	assert(m.ram[SF_TARGET] == 51 && m.ram[SF_OVERRIDE] == 0);
	assert(sf_release(&io) == -EIO); /* Never claim success while releasing. */
	m.ram[SF_EFFECTIVE] = 40;
	assert(sf_release(&io) == 0 && !io.touched);
	io = setup(&m, true);
	m.ram[SF_OVERRIDE] = 1; /* Unsupported state: do not guess a repair. */
	assert(sf_release(&io) == -EIO && m.writes == 0);
	io = setup(&m, true);
	m.ram[SF_OVERRIDE] = m.ram[SF_EFFECTIVE] = 0xff;
	/* Explicit restore of a known leftover does not need a prior takeover. */
	assert(sf_release(&io) == 0 && m.writes == 2);
}

static void stepping(void)
{
	int minimum, goal, current, steps;

	for (minimum = 1; minimum <= 8; minimum++) {
		for (goal = 0; goal <= 51; goal++) {
			if (goal && goal < minimum)
				continue;
			current = 0;
			for (steps = 0; current != goal && steps < 100; steps++) {
				current = sf_next_code(current, goal, minimum);
				assert(current == 0 || (current >= minimum && current <= 51));
			}
			assert(current == goal);
			for (steps = 0; current && steps < 100; steps++)
				current = sf_next_code(current, 0, minimum);
			assert(current == 0);
		}
	}
	assert(sf_next_code(0, 0, 8) == 0);
	assert(sf_next_code(0, 8, 8) == 12);
	assert(sf_next_code(8, 0, 8) == 0);
}

static void quieter_startup(void)
{
	static const int expected[] = { 12, 11, 10, 9, 8, 7, 6 };
	struct mock m;
	struct sf_io io = setup(&m, true);
	int current, next;
	size_t i;

	assert(sf_engage(&io, &current) == 0);
	assert(sf_set(&io, current, 0) == 0);
	current = 0;
	for (i = 0; i < sizeof(expected) / sizeof(expected[0]); i++) {
		next = sf_next_code(current, 6, 5);
		assert(next == expected[i]);
		assert(sf_set(&io, current, next) == 0);
		assert(m.ram[SF_TARGET] == expected[i]);
		current = next;
	}
	assert(sf_next_code(current, 6, 5) == 6); /* Refresh never restarts assist. */
	assert(sf_next_code(current, 10, 5) == 7); /* Already moving: just ramp. */
	assert(sf_next_code(current, 0, 5) == 5);
	assert(sf_next_code(5, 0, 5) == 0); /* Stop never requests assist. */
	assert(sf_next_code(0, 0, 5) == 0);
	assert(sf_next_code(0, 10, 5) == 12);
	assert(sf_next_code(0, 51, 5) == 12); /* Higher goals still ramp upward. */
	assert(sf_release(&io) == 0);
	assert(!io.touched && m.ram[SF_OVERRIDE] == 0);
}

int main(void)
{
	allowlists();
	observation();
	normal_control();
	guards();
	failures_and_recovery();
	stepping();
	quieter_startup();
	puts("mock EC: allowlists, observation, control, restore, failures, stepping and 1200-RPM startup passed");
	return 0;
}
