---
paths:
  - "usb_speed_tui.py"
---

# usb_speed_tui.py

Measures the speed of external drives: sequential write, sequential read, and 4 KiB random read
and write for `RANDOM_SECONDS` each, against one temporary file (`.usb_speed_test.tmp` in the
volume root). The tests run on a worker thread (`Bench`) so the screen stays live; the UI only
reads its fields, and `Bench.stop()` is how anything ends a run early, `q` included.

- **The numbers are only honest because of two fcntls.** `F_NOCACHE` on every descriptor keeps
  the page cache out of it (without it the file just written is read back from RAM), and every
  write phase ends with `F_FULLFSYNC` *inside* the timed span, so "written" means on the
  device and not in the drive's cache. Remove either and the results look better and are wrong.
- **The test file is always removed**, in `Bench.run()`'s `finally`, which covers cancel,
  errors and quitting mid-run. Its size is shrunk to fit the free space (`test_bytes()`) and
  capped at 2 GiB because FAT32 sticks cannot hold a 4 GiB file.
- Drives come from `diskutil info -plist` per `/Volumes` entry: internal volumes and disk
  images are hidden unless `--all`. The USB link speed comes from the IORegistry
  (`ioreg -a -r -c IOUSBHostDevice`), walking down from each USB device to the `BSD Name`
  of its disks; `UsbLinkSpeed` first, `Device Speed` as a fallback. A 480 Mb/s link gets a
  warning, since a USB 3 stick in a USB 2 port or cable is the commonest reason it is slow.

Testing without a USB stick: `hdiutil create -size 600m -fs ExFAT -volname USBTEST t.dmg`,
attach it, and use `--all` (a disk image is otherwise hidden). `--once /Volumes/USBTEST` runs
the whole benchmark headless and prints the results; the curses half works in a tmux pane with
`capture-pane -p`, at 80×24 the smallest size worth checking. The link-speed column can only
be checked with a real USB drive attached.
