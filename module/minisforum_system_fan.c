// SPDX-License-Identifier: GPL-2.0
/* F7BSI system-fan-only variant. No CPU fan registers or controls.
 * Modified 2026-11-01..06 for the UM870 Slim; see NOTICE.md for provenance.
 * I2EC access adapted from mordilloSan/fan_control, commit 5d0845a7,
 * module/minisforum_ec.c, and its cited FanControl.MinisforumUM d61e46c.
 * This module does not flash firmware.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/acpi.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/hwmon.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/jiffies.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/workqueue.h>

#include "system_fan_core.h"

#define NAME "minisforum_system_fan"
#define MONITOR (2 * HZ)
#define STEP (4 * HZ)
#define WATCHDOG (10 * HZ)
#define SETTLE (10 * HZ)
#define EC_LIMIT 55

static bool allow_control;
module_param(allow_control, bool, 0444);
MODULE_PARM_DESC(allow_control, "Enable system-fan writes; default false (observation only)");

static unsigned int min_rpm = 800;
module_param(min_rpm, uint, 0444);
MODULE_PARM_DESC(min_rpm, "System-fan minimum: 100..800 in steps of 100; below 800 is experimental");

struct sample {
	struct sf_regs regs;
	u8 temp, duty;
	int rpm;
};

struct system_fan {
	struct mutex lock;
	struct sf_io io;
	struct delayed_work monitor;
	bool active, pending, fault, stopped, suspended;
	int goal, applied, stalls;
	int window[6];
	unsigned int samples, position;
	unsigned long deadline, next_step, settle_until;
};

static struct platform_device *fan_device;

/* All port accesses are serialized by lock, with ports exclusively reserved.
 * Reads necessarily write address-selection latches, not fan-control data.
 */
static void sio_write(u8 reg, u8 value)
{
	outb(reg, 0x2e);
	outb(value, 0x2f);
}

static u8 sio_read(u8 reg)
{
	outb(reg, 0x2e);
	return inb(0x2f);
}

static void d2_write(u8 reg, u8 value)
{
	sio_write(0x2e, reg);
	sio_write(0x2f, value);
}

static void park(void)
{
	sio_write(0x2e, 0x10);
	outb(0x20, 0x2e);
}

static int port_read(void *ctx, unsigned short addr, unsigned char *value)
{
	if (!sf_can_read(addr))
		return -EPERM;
	d2_write(0x11, addr >> 8);
	d2_write(0x10, addr & 0xff);
	sio_write(0x2e, 0x12);
	*value = sio_read(0x2f);
	park();
	return 0;
}

/* The ONLY EC-data write primitive, with a second hard allowlist check.
 * CPU bases/slopes/overrides and direct PWM registers cannot reach outb here.
 */
static int port_write(void *ctx, unsigned short addr, unsigned char value)
{
	struct system_fan *d = ctx;

	if (!d->io.enabled || !sf_can_write(addr, value))
		return -EPERM;
	d2_write(0x11, addr >> 8);
	d2_write(0x10, addr & 0xff);
	sio_write(0x2e, 0x12);
	sio_write(0x2f, value);
	park();
	return 0;
}

static int controller_identity(void *ctx)
{
	static const u16 addresses[] = { 0x2000, 0x2001, 0x2002, 0x200d, 0x180c, 0x1841 };
	u8 expected[] = { 0x55, 0x71, 0, 0x43, 0x14, 0x7f };
	u8 pnp[3], value;
	unsigned int i;
	int ret;

	for (i = 0; i < ARRAY_SIZE(pnp); i++) {
		pnp[i] = sio_read(0x20 + i);
		outb(0x20, 0x2e);
	}
	if (pnp[0] != 0x55 || pnp[1] != 0x71)
		return -ENODEV;
	expected[2] = pnp[2];
	for (i = 0; i < ARRAY_SIZE(addresses); i++) {
		ret = port_read(ctx, addresses[i], &value);
		if (ret)
			return ret;
		if (value != expected[i])
			return -ENODEV;
	}
	return 0;
}

static void pause_ms(void *ctx, unsigned int ms)
{
	msleep(ms);
}

static int telemetry(struct system_fan *d, struct sample *s)
{
	u8 lo, high, again;
	u16 counter;
	int ret, attempt;

	ret = sf_read_regs(&d->io, &s->regs);
	if (!ret)
		ret = port_read(d, 0x0305, &s->temp);
	if (!ret)
		ret = port_read(d, 0x1804, &s->duty);
	if (ret)
		return ret;
	for (attempt = 0; attempt < 3; attempt++) {
		ret = port_read(d, 0x1820, &lo);
		if (!ret)
			ret = port_read(d, 0x1821, &high);
		if (!ret)
			ret = port_read(d, 0x1820, &again);
		if (ret)
			return ret;
		if (lo != again)
			continue;
		counter = lo | high << 8;
		s->rpm = counter ? 2156250 / counter : 0;
		return 0;
	}
	return -EIO;
}

/* Caller holds lock. Successful restoration is verified, not just requested. */
static int restore_locked(struct system_fan *d)
{
	int ret = sf_release(&d->io);

	d->active = false;
	d->goal = -1;
	d->pending = ret != 0;
	if (!ret) {
		d->applied = -1;
		d->samples = 0;
		d->stalls = 0;
	} else {
		pr_err_ratelimited("system fan restoration failed (%d); keep module loaded for retries; fully power off if recovery fails\n", ret);
	}
	return ret;
}

static void fault_locked(struct system_fan *d, const char *reason)
{
	pr_warn("%s; returning ONLY the system fan to firmware\n", reason);
	d->fault = true;
	restore_locked(d);
}

static bool held(struct system_fan *d, const struct sf_regs *r)
{
	return r->effective == 0xff && r->override == 0xff && r->target == d->applied;
}

static void monitor(struct work_struct *work)
{
	struct system_fan *d = container_of(to_delayed_work(work), struct system_fan, monitor);
	struct sample s;
	int next, low, high;
	unsigned int i;

	guard(mutex)(&d->lock);
	if (d->stopped || d->suspended)
		return;
	if (d->pending) {
		restore_locked(d);
		goto again;
	}
	if (!d->active)
		goto again;
	if (time_after_eq(jiffies, d->deadline)) {
		fault_locked(d, "10-second userspace watchdog expired");
		goto again;
	}
	if (telemetry(d, &s)) {
		fault_locked(d, "unreadable system-fan telemetry");
		goto again;
	}
	if (!sf_plausible(s.temp) || s.temp >= EC_LIMIT) {
		fault_locked(d, "EC system temperature invalid or >=55 C");
		goto again;
	}
	if (!held(d, &s.regs)) {
		fault_locked(d, "system-fan ownership or target changed unexpectedly");
		goto again;
	}
	if (d->applied != d->goal) {
		if (time_before(jiffies, d->next_step))
			goto again;
		next = sf_next_code(d->applied, d->goal, min_rpm / 100);
		if (sf_set(&d->io, d->applied, next)) {
			fault_locked(d, "system-fan target write failed");
			goto again;
		}
		d->applied = next;
		d->next_step = jiffies + STEP;
		d->settle_until = jiffies + SETTLE;
		d->samples = 0;
		d->stalls = 0;
		goto again;
	}
	if (!d->goal || time_before(jiffies, d->settle_until))
		goto again;
	if (!s.rpm) {
		if (++d->stalls >= 3)
			fault_locked(d, "system fan stalled at a nonzero target");
		goto again;
	}
	d->stalls = 0;
	d->window[d->position] = s.rpm;
	d->position = (d->position + 1) % ARRAY_SIZE(d->window);
	if (d->samples < ARRAY_SIZE(d->window))
		d->samples++;
	if (d->samples != ARRAY_SIZE(d->window))
		goto again;
	low = high = d->window[0];
	for (i = 1; i < ARRAY_SIZE(d->window); i++) {
		low = min(low, d->window[i]);
		high = max(high, d->window[i]);
	}
	if (high - low > 600)
		fault_locked(d, "system fan hunting (spread >600 RPM)");
again:
	queue_delayed_work(system_dfl_wq, &d->monitor, MONITOR);
}

static ssize_t target_rpm_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct system_fan *d = dev_get_drvdata(dev);

	guard(mutex)(&d->lock);
	return sysfs_emit(buf, "%d\n", d->goal < 0 ? -1 : d->goal * 100);
}

static ssize_t target_rpm_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct system_fan *d = dev_get_drvdata(dev);
	struct sample s;
	int rpm, code, ret;

	if (kstrtoint(buf, 10, &rpm) || rpm < 0 || rpm > SF_MAX_CODE * 100 ||
	    rpm % 100 || (rpm && rpm < min_rpm))
		return -EINVAL;
	guard(mutex)(&d->lock);
	if (!d->io.enabled)
		return -EPERM;
	if (d->stopped || d->suspended)
		return -EBUSY;
	if (d->fault || d->pending)
		return -EIO; /* Explicit restore is required to acknowledge a fault. */
	ret = telemetry(d, &s);
	if (ret || !sf_plausible(s.temp) || s.temp >= EC_LIMIT) {
		if (d->active)
			fault_locked(d, "telemetry or temperature check failed on target request");
		return ret ?: -ERANGE;
	}
	if (d->active && !held(d, &s.regs)) {
		fault_locked(d, "ownership changed on target request");
		return -EIO;
	}
	if (!d->active) {
		ret = sf_engage(&d->io, &code);
		if (ret) {
			if (d->io.touched)
				fault_locked(d, "taking system-fan control failed");
			return ret;
		}
		d->active = true;
		d->applied = code;
		d->next_step = jiffies;
		d->settle_until = jiffies + SETTLE;
		d->samples = 0;
		d->stalls = 0;
	}
	d->goal = rpm / 100;
	d->deadline = jiffies + WATCHDOG;
	mod_delayed_work(system_dfl_wq, &d->monitor, 0);
	return count;
}
static DEVICE_ATTR_RW(target_rpm);

static ssize_t restore_store(struct device *dev, struct device_attribute *attr,
			     const char *buf, size_t count)
{
	struct system_fan *d = dev_get_drvdata(dev);
	int ret;

	if (!sysfs_streq(buf, "1"))
		return -EINVAL;
	guard(mutex)(&d->lock);
	if (!d->io.enabled)
		return -EPERM;
	if (d->stopped || d->suspended)
		return -EBUSY;
	ret = restore_locked(d);
	if (ret) {
		d->fault = true;
		return ret;
	}
	d->fault = false;
	return count;
}
static DEVICE_ATTR_WO(restore);

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct system_fan *d = dev_get_drvdata(dev);
	struct sample s;
	enum sf_state state;
	const char *name;
	int ret;

	guard(mutex)(&d->lock);
	if (d->stopped || d->suspended)
		return -EBUSY;
	ret = telemetry(d, &s);
	if (ret)
		return ret;
	state = sf_classify(&s.regs);
	if (d->pending)
		name = "release-pending";
	else if (state == SF_FIRMWARE)
		name = "firmware";
	else if (state == SF_FIXED)
		name = d->active ? "controlled" : "external-fixed";
	else if (state == SF_RELEASING)
		name = "releasing";
	else
		name = "unsupported";
	return sysfs_emit(buf,
		"state=%s\nfirmware_owned=%u\nrelease_pending=%u\nfault=%u\nallow_control=%u\nmin_rpm=%u\nrequested_rpm=%d\napplied_rpm=%u\nfan_rpm=%d\nec_temp_millidegrees=%u\npwm_duty=%u\noverride=%u\neffective=%u\n",
		name, state == SF_FIRMWARE, d->pending, d->fault, d->io.enabled,
		min_rpm, d->goal < 0 ? -1 : d->goal * 100, s.regs.target * 100,
		s.rpm, s.temp * 1000, s.duty, s.regs.override, s.regs.effective);
}
static DEVICE_ATTR_RO(status);

static struct attribute *fan_attrs[] = {
	&dev_attr_target_rpm.attr, &dev_attr_restore.attr, &dev_attr_status.attr, NULL,
};
ATTRIBUTE_GROUPS(fan);

static umode_t visible(const void *data, enum hwmon_sensor_types type, u32 attr, int channel)
{
	return 0444;
}

static int sensor_read(struct device *dev, enum hwmon_sensor_types type,
		       u32 attr, int channel, long *value)
{
	struct system_fan *d = dev_get_drvdata(dev);
	struct sample s;
	int ret;

	guard(mutex)(&d->lock);
	if (d->stopped || d->suspended)
		return -EBUSY;
	ret = telemetry(d, &s);
	if (ret)
		return ret;
	if (type == hwmon_fan)
		*value = s.rpm;
	else if (type == hwmon_temp)
		*value = s.temp * 1000;
	else
		return -EOPNOTSUPP;
	return 0;
}

static int sensor_label(struct device *dev, enum hwmon_sensor_types type,
			u32 attr, int channel, const char **value)
{
	*value = type == hwmon_fan ? "System (not CPU)" : "EC system (not RAM)";
	return 0;
}

static const struct hwmon_ops sensor_ops = {
	.is_visible = visible, .read = sensor_read, .read_string = sensor_label,
};
static const struct hwmon_channel_info * const channels[] = {
	HWMON_CHANNEL_INFO(fan, HWMON_F_INPUT | HWMON_F_LABEL),
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT | HWMON_T_LABEL), NULL,
};
static const struct hwmon_chip_info chip_info = { .ops = &sensor_ops, .info = channels };

static void stop(void *data)
{
	struct system_fan *d = data;
	int attempt;

	scoped_guard(mutex, &d->lock)
		d->stopped = true;
	cancel_delayed_work_sync(&d->monitor);
	guard(mutex)(&d->lock);
	if (!d->io.touched && !d->pending)
		return;
	for (attempt = 0; attempt < 3; attempt++) {
		if (!restore_locked(d))
			return;
		msleep(200);
	}
	pr_crit("SYSTEM FAN RESTORE FAILED: fully power off and disconnect power before further EC use; warm reboot is not a guaranteed reset\n");
}

static int probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct system_fan *d;
	struct sf_regs regs;
	struct device *hwmon;
	int ret;

	if (acpi_check_region(0x2e, 2, NAME) || !devm_request_region(dev, 0x2e, 2, NAME))
		return -EBUSY;
	d = devm_kzalloc(dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	ret = devm_mutex_init(dev, &d->lock);
	if (ret)
		return ret;
	d->io = (struct sf_io){
		.ctx = d, .enabled = allow_control, .read = port_read, .write = port_write,
		.identity = controller_identity, .pause = pause_ms,
	};
	d->goal = d->applied = -1;
	INIT_DELAYED_WORK(&d->monitor, monitor);
	ret = devm_add_action_or_reset(dev, stop, d);
	if (ret)
		return ret;
	scoped_guard(mutex, &d->lock) {
		ret = controller_identity(d);
		if (!ret)
			ret = sf_policy(&d->io);
		if (!ret)
			ret = sf_read_regs(&d->io, &regs);
	}
	if (ret || sf_classify(&regs) == SF_UNSUPPORTED)
		return ret ?: -ENODEV;
	/* No automatic repair or takeover, even with allow_control=1. */
	platform_set_drvdata(pdev, d);
	hwmon = devm_hwmon_device_register_with_info(dev, NAME, d, &chip_info, fan_groups);
	if (IS_ERR(hwmon))
		return PTR_ERR(hwmon);
	pr_info("system fan ONLY; CPU EC addresses inaccessible; writes %s; minimum %u RPM; no automatic takeover\n",
		allow_control ? "enabled" : "BLOCKED", min_rpm);
	queue_delayed_work(system_dfl_wq, &d->monitor, MONITOR);
	return 0;
}

static int suspend(struct device *dev)
{
	struct system_fan *d = dev_get_drvdata(dev);
	int ret = 0;

	scoped_guard(mutex, &d->lock)
		d->suspended = true;
	cancel_delayed_work_sync(&d->monitor);
	scoped_guard(mutex, &d->lock) {
		if (d->io.touched || d->pending)
			ret = restore_locked(d);
		if (ret)
			d->suspended = false;
	}
	if (ret)
		mod_delayed_work(system_dfl_wq, &d->monitor, 0);
	return ret;
}

static int resume(struct device *dev)
{
	struct system_fan *d = dev_get_drvdata(dev);

	scoped_guard(mutex, &d->lock) {
		d->suspended = false;
		/* Never resume raw control implicitly. The foreground controller exits
		 * when it sees this fault; an explicit restore acknowledges it.
		 */
		if (d->io.enabled)
			d->fault = true;
	}
	mod_delayed_work(system_dfl_wq, &d->monitor, 5 * HZ);
	return 0;
}
static DEFINE_SIMPLE_DEV_PM_OPS(pm_ops, suspend, resume);

static void shutdown(struct platform_device *pdev)
{
	struct system_fan *d = platform_get_drvdata(pdev);

	if (d)
		stop(d);
}

static struct platform_driver driver = {
	.driver = { .name = NAME, .pm = pm_sleep_ptr(&pm_ops) }, .shutdown = shutdown,
};

struct ec_version { bool found; u8 major, minor; };

static void decode_version(const struct dmi_header *header, void *data)
{
	struct ec_version *version = data;
	const u8 *raw = (const u8 *)header;

	if (header->type != 0 || version->found)
		return;
	version->found = true;
	version->major = header->length >= 0x18 ? raw[0x16] : 0xff;
	version->minor = header->length >= 0x18 ? raw[0x17] : 0xff;
}

static int __init system_fan_init(void)
{
	static const struct { int field; const char *value; } required[] = {
		{ DMI_PRODUCT_NAME, "EliteMini Series" }, { DMI_PRODUCT_VERSION, "1.0" },
		{ DMI_PRODUCT_FAMILY, "EliteMini" }, { DMI_PRODUCT_SKU, "MGF7BSI" },
		{ DMI_BOARD_NAME, "F7BSI" }, { DMI_BOARD_VERSION, "1.0" },
	};
	struct ec_version version = { };
	const char *value;
	unsigned int i;

	if (min_rpm < 100 || min_rpm > 800 || min_rpm % 100)
		return -EINVAL;
	/* Reject identity mismatches BEFORE any port access. No force option. */
	for (i = 0; i < ARRAY_SIZE(required); i++) {
		value = dmi_get_system_info(required[i].field);
		if (!value || strcmp(value, required[i].value))
			return -ENODEV;
	}
	if (dmi_walk(decode_version, &version) || !version.found ||
	    version.major != 0 || version.minor != 5)
		return -ENODEV;
	fan_device = platform_create_bundle(&driver, probe, NULL, 0, NULL, 0);
	return PTR_ERR_OR_ZERO(fan_device);
}

static void __exit system_fan_exit(void)
{
	platform_device_unregister(fan_device);
	platform_driver_unregister(&driver);
}

module_init(system_fan_init);
module_exit(system_fan_exit);
MODULE_LICENSE("GPL");
/* Bump for shared-core behaviour changes too, so loaded builds stay distinct. */
MODULE_VERSION("0.1.1");
MODULE_DESCRIPTION("Temporary F7BSI system-fan-only control; CPU writes prohibited");
