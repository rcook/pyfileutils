#!/usr/bin/env python
# Deduper
# A reasonably useful file deduplicator
# https://github.com/rcook/deduper

import argparse
import datetime
import hashlib
import logging
import os
import sys

##################################################

class DoNotRemoveDuplicatesStrategy(object):
    NAME = "nop"

    def apply(self, paths):
        return paths, []

    def __repr__(self): return self.NAME

class KeepFirstInCopyAwareOrderStrategy(object):
    NAME = "keep-first"

    def apply(self, paths):
        sorted_paths = sorted(paths, cmp=self.copy_aware_path_compare)
        return [sorted_paths[0]], sorted_paths[1:]

    def __repr__(self): return self.NAME

    @staticmethod
    def has_prefix(prefix, file_name):
        if file_name.startswith(prefix):
            return True, file_name[len(prefix):]
        return False, file_name

    @staticmethod
    def copy_aware_path_compare(p0, p1):
        d0 = os.path.dirname(p0)
        d1 = os.path.dirname(p1)
        if d0 != d1:
            return cmp(p0, p1)

        n0 = os.path.basename(p0)
        result0, b0 = KeepFirstInCopyAwareOrderStrategy.has_prefix("Copy of ", n0)
        n1 = os.path.basename(p1)
        result1, b1 = KeepFirstInCopyAwareOrderStrategy.has_prefix("Copy of ", n1)

        if b0 == b1:
            if result0:
                return 1
            if result1:
                return -1
            raise RuntimeError("Unreachable")

        return cmp(n0, n1)

##################################################

PROGRESS_STEP = 1

BLOCK_SIZE = 1024

GIB_THRESHOLD = 1024 * 1024 * 1024
MIB_THRESHOLD = 1024 * 1024

DEFAULT_STRATEGY = DoNotRemoveDuplicatesStrategy()
STRATEGIES = [
    DEFAULT_STRATEGY,
    KeepFirstInCopyAwareOrderStrategy()
]

##################################################

class Progress(object):
    def __init__(self, is_enabled):
        self._is_enabled = is_enabled
        self._i = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._is_enabled:
            sys.stdout.write("\n")

    def step(self):
        if self._is_enabled:
            if self._i % PROGRESS_STEP == 0:
                sys.stdout.write(".")
                sys.stdout.flush()
            self._i += 1

def debug_logging():
    return logging.getLogger().isEnabledFor(logging.DEBUG)

def pretty_list(items):
    return "(empty)" if len(items) == 0 else ", ".join(items)

def pretty_byte_count(n):
    if n >= GIB_THRESHOLD:
        return "{0:0.1f} GiB".format(float(n) / GIB_THRESHOLD)
    elif n >= MIB_THRESHOLD:
        return "{0:0.1f} MiB".format(float(n) / MIB_THRESHOLD)
    else:
        return "{} bytes".format(n)

def dump(root_dir, d):
    # Potentially expensive logging
    if debug_logging():
        entries = ["{}: {}".format(key, pretty_list(map(lambda p: os.path.relpath(p, root_dir), paths))) for key, paths in d.iteritems()]
        logging.debug("Files: {}".format(pretty_list(entries)))

def compare_files(p0, p1):
    with open(p0, "rb") as f:
        d0 = f.read()
    with open(p1, "rb") as f:
        d1 = f.read()

    return d0 == d1

def compute_file_count(d):
    file_count = sum([len(paths) for _, paths in d.iteritems()])
    return file_count

def compute_wastage(d, debug):
    duplicate_file_count = sum([len(paths) - 1 for _, paths in d.iteritems()])
    duplicate_byte_count = sum([os.stat(paths[0]).st_size * (len(paths) - 1) for _, paths in d.iteritems()])

    if debug:
        is_valid = True
        for _, paths in d.iteritems():
            p0 = paths[0]
            for p in paths:
                if not compare_files(p0, p):
                    is_valid = False
                    logging.info("File comparison failed: {} vs {}".format(p0, p))

        if not is_valid:
            raise RuntimeError("Diagnostics failed")

    return duplicate_file_count, duplicate_byte_count

def prune(d):
    return { k : paths for k, paths in d.iteritems() if len(paths) > 1 }

##################################################

def find_duplicates(root_dir, strategy, dry_run, debug, show_progress):
    size_map = scan(root_dir, show_progress=show_progress)
    dump(root_dir, size_map)

    logging.info("Size scan found {} candidate files".format(compute_file_count(size_map)))

    sig_map0 = compute_signatures(size_map, partial=True, show_progress=show_progress)
    dump(root_dir, sig_map0)

    logging.info("Signature scan found {} candidate files".format(compute_file_count(sig_map0)))

    sig_map1 = compute_signatures(sig_map0, partial=False, show_progress=show_progress)
    dump(root_dir, sig_map1)

    duplicate_file_count, duplicate_byte_count = compute_wastage(sig_map1, debug=debug)
    logging.info("Found {} duplicate files occupying {}".format(
        duplicate_file_count,
        pretty_byte_count(duplicate_byte_count)))

    bytes_freed = 0
    file_count = 0
    for _, paths in sig_map1.iteritems():
        files_to_keep, files_to_remove = strategy.apply(paths)

        # Potentially expensive logging
        if debug_logging():
            logging.debug("{}: files to keep: {}, files to remove: {}".format(
                type(strategy).__name__,
                pretty_list(files_to_keep),
                pretty_list(files_to_remove)))

        file_count += len(files_to_remove)
        for path in files_to_remove:
            logging.debug("Removing {}".format(path))
            bytes_freed += os.stat(path).st_size
            if not dry_run:
                os.unlink(path)

    logging.info("Strategy \"{}\" deleted {} files and freed {}".format(
        strategy.NAME,
        file_count,
        pretty_byte_count(bytes_freed)))

def scan(start_dir, show_progress=False):
    with Progress(show_progress) as p:
        result = {}
        for root_dir, _, file_names in os.walk(start_dir):
            for file_name in file_names:
                p.step()
                path = os.path.join(root_dir, file_name)
                file_size = os.stat(path).st_size
                if file_size not in result:
                    result[file_size] = []
                result[file_size].append(path)

    return prune(result)

def compute_signatures(d, partial=False, show_progress=False):
    with Progress(show_progress) as p:
        result = {}
        for _, paths in d.iteritems():
            for path in paths:
                p.step()
                sig = compute_signature(path, partial)
                if sig not in result:
                    result[sig] = []
                result[sig].append(path)

    return prune(result)

def compute_signature(path, partial=False):
    file_size = os.stat(path).st_size
    if partial:
        block_count = 1
    else:
        block_count = (file_size / BLOCK_SIZE) + 1 if (file_size % BLOCK_SIZE) > 0 else 0

    m = hashlib.sha1()
    with open(path, "rb") as f:
        for i in range(0, block_count):
            m.update(f.read(BLOCK_SIZE))

    return "{}:{}".format(file_size, m.hexdigest())

##################################################

def get_strategy(name):
    strategy = next((s for s in STRATEGIES if s.NAME == name), None)
    if strategy is None:
        raise argparse.ArgumentTypeError("Strategy must be one of ({})".format(", ".join(sorted(map(lambda s: "\"{}\"".format(s.NAME), STRATEGIES)))))
    return strategy

def is_safe_dir(path):
    parts = path.strip("/").split("/")
    return len(parts) >= 3

def add_switch_with_inverse(parser, name, default, help=None, inverse_help=None):
    group = parser.add_mutually_exclusive_group()
    dest = name.replace("-", "_")
    group.add_argument(
        "--{}".format(name),
        dest=dest,
        action="store_true",
        default=default,
        help=help)
    group.add_argument(
        "--no-{}".format(name),
        dest=dest,
        action="store_false",
        default=default,
        help=inverse_help)

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        description="A reasonably useful file deduplicator",
        epilog="https://github.com/rcook/deduper")
    parser.add_argument(
        "root_dir",
        metavar="ROOTDIR",
        type=os.path.abspath,
        help="start directory for scan")
    parser.add_argument(
        "--strategy",
        dest="strategy",
        type=get_strategy,
        default=DEFAULT_STRATEGY,
        help="deduplication strategy to employ")
    add_switch_with_inverse(
        parser,
        "dry-run",
        default=True,
        help="perform scan but do not delete files",
        inverse_help="perform scan and delete files")
    add_switch_with_inverse(
        parser,
        "verbose",
        default=False,
        help="show extra logging",
        inverse_help="do not show extra logging")
    add_switch_with_inverse(
        parser,
        "debug",
        default=False,
        help="show extra diagnostics",
        inverse_help="do not show extra diagnostics")
    add_switch_with_inverse(
        parser,
        "force",
        default=False,
        help="override safety check on protected directories",
        inverse_help="do not override safety check on protected directories")
    add_switch_with_inverse(
        parser,
        "progress",
        default=False,
        help="show progress",
        inverse_help="do not show progress")

    args = parser.parse_args(argv)

    if not args.force and not is_safe_dir(args.root_dir):
        sys.stderr.write("Safety check failed: do you really want to run this command on this directory?\n")
        sys.exit(1)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s:%(message)s")

    for k, v in sorted(vars(args).iteritems()):
        logging.info("Argument: {}={}".format(k, v))

    start_time = datetime.datetime.now()
    logging.info("Scan started at {}".format(start_time))
    find_duplicates(
        args.root_dir,
        args.strategy,
        dry_run=args.dry_run,
        debug=args.debug,
        show_progress=args.progress)
    end_time = datetime.datetime.now()
    logging.info("Scan finished at {}, elapsed time: {}".format(end_time, end_time - start_time))

##################################################

if __name__ == "__main__":
    main()
