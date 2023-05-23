#!/bin/python3
import os, argparse, sys, json
from pathlib import Path
from dataclasses import dataclass
from filecmp import cmp as filecmp
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from shutil import copy2, rmtree

def command_line_args():
    parser = argparse.ArgumentParser(
      description="Generates the image variants for GHPages serving",
      epilog="Note: Assumes that the imgs repo already exists at $PWD/../imgs",
      formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-d", "--dest",
      help="The folder you want everything to end up in",
      type=Path, default=Path('../site'))
    parser.add_argument("-c", "--cores",
      help="Number of worker threads to use",
      default=os.cpu_count(), type=int)
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

def touch_file(path):
    """Returns True if the file was in `files_to_rm`"""
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
    take_action(lambda:rmtree(file), f"rm -rf {str(file)}")
  else:
    take_action(lambda:file.unlink(), f"rm {str(file)}")

@dataclass(frozen=True)
class ImageVariant:
    command: str
    outpath: str

def magick_resize(width: int, height: int, target_width: int, target_height: int, center_x, center_y):
    """
    Returns an imagemagick command for resizing an image buffer
    from `width` x `height` to `target_width` x `target_height`
    focusing on the given center point
    
    Params:
    - widths and heights are in pixels and must be integers
    - center is in percentage points (i.e. 50 is the middle)
    
    Returns:
    A string giving the -resize command without any additional IO ops
    """
    target_ratio = float(target_width) / float(target_height)
    actual_ratio = float(width) / float(height)
    crop_width = width
    crop_height = height
    crop_x = 0
    crop_y = 0
    if target_ratio > actual_ratio: # Wider target => trim top/bottom
        crop_height = round(height * actual_ratio / target_ratio)
        delta_h = height - crop_height
        crop_y = round(delta_h * center_y / 100.0)
    if target_ratio < actual_ratio: # Taller target => trim sides
        crop_width = round(width * target_ratio / actual_ratio)
        delta_w = width - crop_width
        crop_x = round(delta_w * center_x / 100.0)
    ret = ""
    if crop_height != height or crop_width != width:
        ret = f"-crop '{crop_width}x{crop_height}+{crop_x}+{crop_y}' +repage "
    if target_height != crop_height or target_width != crop_width:
        ret += f"-resize '{target_width}x{target_height}>'"
    return ret

class BaseImageDeriver:
    # method is 0-6 = fast-quality
    # pass is number of passes to iteratively approach the target-psnr. Should be btw 3 and 7
    # strip removes metadata
    MAGICK_OPTS = "-verbose -strip -define webp:method=6 -define webp:pass=5 -define webp:target-psnr=49"
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
        self.metadata = {'version': self.VERSION}
        self.modified_files = []

    def getSourceFiles(self):
        return self.source.iterdir()

    def getVariantsForImage(self, filename, width, height):
        raise NotImplementedError

    def versionMatches(self, inpath, outpath):
        return self.previous_info['version'] >= self.VERSION

    def magickCmdForFile(self, file):
        result = subprocess.run(f"identify -ping -format '[%w,%h]' \"{str(file)}\"",
          shell=True, check=True, capture_output=True)
        width, height = json.loads(result.stdout)
        relfile = file.relative_to(self.source)
        variants = self.getVariantsForImage(relfile, width, height)
        cmd = ""
        for variant in variants:
            outpath = self.target/variant.outpath
            touch_file(outpath)
            if outpath.exists():
              if self.versionMatches(relfile, outpath):
                continue
              self.modified_files.append(outpath)
            cmd += f" {variant.command} -write \"{str(outpath)}\""
        if cmd == "":
            return None
        # ImageMagick requires that the last output file _not_ get the explicit " -write " command
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
          with ThreadPoolExecutor(max_workers=args.cores) as executor:
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
          take_action(lambda: self.metadata_path.write_text(json.dumps(self.metadata)),
            f"Dumping {self.__class__.__name__}'s metadata to a json file...")
        if done == 0:
            print(f"Nothing to do :_)")
        print(f"===Finished {self.__class__.__name__}===\n")

class ImageryCourseImageDeriver(BaseImageDeriver):
    SRC='../imgs/imagery'
    DST='imagery'
    VERSION = 1

    def getVariantsForImage(self, filename, width, height):
        retname = str(filename.with_suffix('.webp'))
        return [
            ImageVariant("-resize '1066x1280>'", retname),
            ImageVariant("-resize 533", retname.replace('.webp', '-1x.webp')),
        ]

class BuddhismCourseImageDeriver(BaseImageDeriver):
    SRC='../imgs/buddhism'
    DST='buddhism'
    VERSION = 1

    def getVariantsForImage(self, filename, width, height):
        retname = str(filename.with_suffix('.webp'))
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

class TagIllustrationImageDeriver(BaseImageDeriver):
    SRC='../imgs/tags'
    MAGICK_OPTS=BaseImageDeriver.MAGICK_OPTS+" -write mpr:orig"
    DST='tags'
    VERSION = 1

    def __init__(self, root, dest, verbose=False, dry_run=False):
        super(TagIllustrationImageDeriver, self).__init__(root, dest, verbose=verbose, dry_run=dry_run)
        image_data_path = self.source/'image_metadata.json'
        self.metadata['image_data'] = json.loads(image_data_path.read_text())

    def getSourceFiles(self):
        return self.source.glob('*/*')

    def getVariantsForImage(self, file, width, height):
        crop = self.metadata['image_data'][file.name]['center']
        crop[0] = round((width-448)*crop[0]/100.0)
        crop[1] = round((height-250)*crop[1]/100.0)
        return [
            ImageVariant("-resize '1840x1250>'", file.stem+'.webp'),
            ImageVariant("-resize '920x625>'", file.stem+'-1x.webp'),
            ImageVariant(
                f"+delete mpr:orig -crop '448x250+{crop[0]}+{crop[1]}'",
                file.stem+'-preview.webp'
            ),
        ]

class BannerImageDeriver(BaseImageDeriver):
    MAGICK_OPTS=BaseImageDeriver.MAGICK_OPTS+" -write mpr:orig"
    SRC='banners'
    DST='banners'
    VERSION = 2
    TARGET_WIDTHS = [400, 594, 881, 1308, 1940, 2880, 4274, 6343]
    DENSITY = 1.5
    # if big_image_width <= target*MIN_DPP_FOR_2x, only 1x available
    # and all larger "target_widths" are skipped
    MIN_DPP_FOR_2X = 1.75
    # the above constants need to be kept in sync with obu/_data/banner.yml

    def __init__(self, root, dest, verbose=False, dry_run=False):
        super(BannerImageDeriver, self).__init__(root, dest, verbose=verbose, dry_run=dry_run)
        image_data_path = self.source/'image_metadata.json'
        self.metadata['image_data'] = json.loads(image_data_path.read_text())

    def versionMatches(self, inpath, outpath):
        return self.previous_info['version'] >= self.VERSION and self.previous_info['image_data'][inpath.name] == self.metadata['image_data'][inpath.name]

    def getHeightForType(self, subfolder):
        match subfolder:
            case 'courses':
                return 680
            case 'footers':
                return 650
            case 'headers':
                return 240
            case 'navbar_headers':
                return 200
            case 'huge_footers':
                return 900
            case _:
                raise ValueError(f"Unexpected subfolder {subfolder} in BannerImageDeriver")

    def getSourceFiles(self):
        return self.source.glob('*/*')

    def getVariantsForImage(self, file, width, height):
        subfolder = file.parts[0]
        target_height = self.getHeightForType(subfolder)
        center = self.metadata['image_data'][file.name]['center']
        ret = []
        for target_width in self.TARGET_WIDTHS:
          big = [self.DENSITY*target_width, self.DENSITY*target_height]
          if target_width*self.MIN_DPP_FOR_2X > width:
            ret.append(ImageVariant(
              "+delete mpr:orig "+magick_resize(
                width, height,
                round(big[0]), round(big[1]),
                center[0], center[1]
              ),
              file.stem+f"-{target_width}-1x.webp"
            ))
            return ret
          else:
            ret.append(ImageVariant(
              ("+delete mpr:orig " if ret else '')+magick_resize(
              width, height,
              round(2*big[0]), round(2*big[1]),
              center[0], center[1]),
              file.stem+f"-{target_width}-2x.webp"
            ))
            ret.append(ImageVariant(
              f"-resize '{round(big[0])}x{round(big[1])}>'",
              file.stem+f"-{target_width}-1x.webp"
            ))
        return ret

def write_modified_file_list(modified_files):
    dest = args.dest.resolve()
    flist = map(lambda f: str(f.relative_to(dest)), modified_files)
    take_action(lambda: (args.dest/'modified_files.txt').write_text('\n'.join(flist)), '> modified_files.txt')

if __name__ == "__main__":
    global args 
    args = command_line_args()
    args.repo_dir = Path(sys.path[0])
    if args.verbose:
        print(f"Running with args: {args}\n")
    prepare_dest(args.dest, args.remove_old)
    copy_file(args.repo_dir/'index.html', args.dest/'index.html')
    modified_files = []
    for deriverclass in [BuddhismCourseImageDeriver, FunctionCourseImageDeriver, ImageryCourseImageDeriver, TagIllustrationImageDeriver, BannerImageDeriver]:
        deriver = deriverclass(
          args.repo_dir, args.dest,
          verbose=args.verbose, dry_run=args.dry_run)
        deriver.run()
        modified_files += deriver.modified_files
    if len(files_to_rm)>0:
      if args.verbose:
        print("\nRemoving old files:")
      remove_untouched_files()
    print("\nModified:")
    print(modified_files)
    if len(modified_files)>0:
        write_modified_file_list(modified_files)

