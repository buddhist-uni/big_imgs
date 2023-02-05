#!/bin/python3
import os, argparse, sys, json
from pathlib import Path
from itertools import chain
from dataclasses import dataclass
from filecmp import cmp as filecmp
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from shutil import copy2

def command_line_args():
    parser = argparse.ArgumentParser(
      description="Generates the image variants for GHPages serving",
      epilog="Note: Assumes that the imgs repo already exists at $PWD/../imgs",
      formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-d", "--dest",
      help="The folder you want everything to end up in",
      type=Path, default=Path('../site'))
    parser.add_argument("-r", "--remove-old",
      help="Delete unnecessary files in DEST?",
      default=False, action='store_true')
    parser.add_argument("--dry-run",
      help="Prints actions but doesn't take them",
      default=False, action='store_true')
    parser.add_argument("--verbose",
      help="Print every action taken along with some explanation",
      default=False, action='store_true')
    return parser.parse_args()

def take_action(action, description):
    if args.verbose or args.dry_run:
        print(f"-{description}")
    if not args.dry_run:
        action()

files_to_rm = set()

def prepare_dest(dest, remove_old):
    if dest.exists():
        if not dest.is_dir():
            print("Destination must be a directory!")
            exit(1)
        if remove_old:
            global files_to_rm
            files_to_rm = set(map(lambda p: p.resolve(), dest.iterdir()))
    else:
        take_action(lambda:args.dest.mkdir(), f"mkdir {str(args.dest)}")

"""Returns True if the file was in `files_to_rm`"""
def touch_file(path):
    try:
        files_to_rm.remove(Path(path).resolve())
        return True
    except KeyError:
        return False

def copy_file(frompath, topath):
    if topath.exists():
        touch_file(topath)
        if filecmp(str(frompath), str(topath), shallow=False):
          if args.verbose:
            print(f"{str(topath)}: already correct")
          return
        take_action(
          lambda:topath.unlink(),
          f"rm {str(topath)}")
    take_action(
      lambda:copy2(frompath,topath),
      f"cp {str(frompath)} {str(topath)}")

def remove_untouched_files():
  if len(files_to_rm)>0:
    for file in files_to_rm:
      rm_file(file)

def rm_file(file):
  if file.is_dir():
    take_action(lambda:file.rmdir(), f"rm -rf {str(file)}")
  else:
    take_action(lambda:file.unlink(), f"rm {str(file)}")

@dataclass(frozen=True)
class ImageVariant:
    command: str
    outpath: str

class BaseImageDeriver:
    MAGICK_OPTS = "-verbose -strip -define webp:method=4 -define webp:pass=5 -define webp:target-psnr=49"
    METADATA_FILENAME = 'metadata.json'
    SRC = NotImplemented
    DST = NotImplemented
    VERSION = NotImplemented

    def __init__(self, root, dest, verbose=False, dry_run=False):
        self.source = (root/self.SRC).resolve()
        self.target = (dest/self.DST).resolve()
        self.verbose = verbose
        self.dry_run = dry_run
        self.metadata_path = self.target/self.METADATA_FILENAME
        try:
            self.previous_info = json.loads(self.metadata_path.read_text())
        except FileNotFoundError:
            self.previous_info = {'version': 0}

    def getSourceFiles(self):
        return self.source.iterdir()

    def getVariantsForImage(self, filename, width, height):
        raise NotImplementedError

    def magickCmdForFile(self, file):
        result = subprocess.run(f"identify -ping -format '[%w,%h]' \"{str(file)}\"",
          shell=True, check=True, capture_output=True)
        width, height = json.loads(result.stdout)
        variants = self.getVariantsForImage(file.name, width, height)
        cmd = ""
        for variant in variants:
            touch_file(variant.outpath)
            if self.previous_info['version'] >= self.VERSION and Path(variant.outpath).exists():
                continue
            cmd += f" {variant.command} -write \"{variant.outpath}\""
        if cmd == "":
            return None
        return f"convert \"{str(file)}\" {self.MAGICK_OPTS}" + ' '.join(cmd.rsplit(' -write ', 1))

    def run(self):
        print(f"\n======={self.__class__.__name__}========")
        if self.verbose:
            print(f"{ str(self.source) } => {str(self.target)}")
        if not self.source.is_dir():
            print(f"{str(self.source)} is not a directory!")
            exit(1)
        if self.target.exists():
            if not self.target.is_dir():
              rm_file(self.target)
              print(f"Warning! {str(self.target)} was not a directory (and was overwritten)")
            touch_file(self.target)
        take_action(
            lambda:self.target.mkdir(parents=True, exist_ok=True),
            f"mkdir -p {str(self.target)}")
        done = 0
        if self.dry_run:
            for file in self.getSourceFiles():
                cmd = self.magickCmdForFile(file)
                if cmd:
                    print(f"-{cmd}")
                    done += 1
        else:
          with ThreadPoolExecutor(max_workers=os.cpu_count()+1) as executor:
            futures = []
            for file in self.getSourceFiles():
              cmd = self.magickCmdForFile(file)
              if cmd:
                done += 1
                futures.append(executor.submit(
                    subprocess.run,
                    cmd,
                    shell=True,
                    capture_output=True
                ))
            for future in as_completed(futures):
                result = future.result()
                print(f"-{result.args}", flush=True)
                if self.verbose or result.returncode:
                    sys.stdout.buffer.write(result.stdout)
                    # imagemagick often writes to stderr too
                    sys.stdout.buffer.write(result.stderr)
                    sys.stdout.buffer.flush()
                if result.returncode:
                    raise subprocess.CalledProcessError(result.returncode, result.args)
          self.metadata_path.write_text(json.dumps({'version': self.VERSION}))
        if done == 0:
            print(f"Nothing to do :_)")
        print(f"===Finished {self.__class__.__name__}===\n")

class ImageryCourseImageDeriver(BaseImageDeriver):
    SRC='../imgs/imagery'
    DST='imagery'
    VERSION = 1

    def getVariantsForImage(self, filename, width, height):
        retname = str((self.target/filename).with_suffix('.webp'))
        return [
            ImageVariant("-resize '1066x1280>'", retname),
            ImageVariant("-resize 533", retname.replace('.webp', '-1x.webp')),
        ]

class BuddhismCourseImageDeriver(BaseImageDeriver):
    SRC='../imgs/buddhism'
    DST='buddhism'
    VERSION = 1

    def getVariantsForImage(self, filename, width, height):
        retname = str((self.target/filename).with_suffix('.webp'))
        if height > width and height >= 1536:
          return [
            ImageVariant("-resize '1920x1920>'", retname),
            ImageVariant("-resize '1280x1280>'", retname.replace('.webp', '-2x.webp')),
            ImageVariant("-resize '640x640>'", retname.replace('.webp', '-1x.webp')),
          ]
        else:
          return [
            ImageVariant("-resize '1280x1280>'", retname),
            ImageVariant("-resize '640x640>'", retname.replace('.webp', '-1x.webp')),
          ]

class FunctionCourseImageDeriver(BuddhismCourseImageDeriver):
    SRC='../imgs/function'
    DST='function'
    VERSION = 1

if __name__ == "__main__":
    global args 
    args = command_line_args()
    args.repo_dir = Path(sys.path[0])
    if args.verbose:
        print(f"Running with args: {args}\n")
    prepare_dest(args.dest, args.remove_old)
    copy_file(args.repo_dir/'index.html', args.dest/'index.html')
    for deriverclass in [BuddhismCourseImageDeriver, FunctionCourseImageDeriver, ImageryCourseImageDeriver]:
        deriver = deriverclass(
          args.repo_dir, args.dest,
          verbose=args.verbose, dry_run=args.dry_run)
        deriver.run()
    if len(files_to_rm)>0:
      if args.verbose:
        print("\nRemoving old files:")
      remove_untouched_files()

