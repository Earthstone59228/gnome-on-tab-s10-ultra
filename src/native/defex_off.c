// defex_off.c — gts10u (SM-X926B, kernel 6.1.145-android14-11-abX926BXXS9DZG1)
//
// Neuter Samsung DEFEX enforcement while loaded: kprobe task_defex_enforce()
// and null its task argument for every call. Applied unconditionally while
// loaded — the scoped equivalent of defex_enforce=0.
// No text patching (MTK MKP safe): kprobe attachment is ftrace-based.
// Reversible: rmmod restores enforcement. Volatile per boot by design.
#include <linux/module.h>
#include <linux/kprobes.h>

static struct kprobe defex_kp = {
	.symbol_name = "task_defex_enforce",
};

static int defex_off_pre(struct kprobe *p, struct pt_regs *regs)
{
	regs->regs[0] = 0; /* task = NULL -> enforcement lookup fails cleanly */
	return 0;
}

static int __init defex_off_init(void)
{
	int ret;

	defex_kp.pre_handler = defex_off_pre;
	ret = register_kprobe(&defex_kp);
	if (ret) {
		pr_err("defex_off: register_kprobe(task_defex_enforce) failed: %d\n", ret);
		return ret;
	}
	pr_info("defex_off: task_defex_enforce neutralized (defex enforcement off)\n");
	return 0;
}

static void __exit defex_off_exit(void)
{
	unregister_kprobe(&defex_kp);
	pr_info("defex_off: unloaded, DEFEX enforcement restored\n");
}

module_init(defex_off_init);
module_exit(defex_off_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("gts10u: DEFEX enforcement off while loaded (kprobe, no text patch)");
