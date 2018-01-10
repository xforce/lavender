"""Generates Visual Studio project files."""

from __future__ import division, print_function, unicode_literals
import argparse
import errno
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BAZEL = 'bazel'

PROJECT_TYPE_GUID = '{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}'

class Label:
    PATTERN = re.compile(r'((@[a-zA-Z0-9/._-]+)?//)?([a-zA-Z0-9/._-]*)(:([a-zA-Z0-9_/.+=,@~-]+))?$')

    def __init__(self, name):
        match = re.match(Label.PATTERN, name)
        if not match:
            raise ValueError("Invalid label: " + name)
        self.repo = match[2] or None
        self._absolute = True if match[1] else False
        if not self._absolute:
            raise NotImplementedError("Absolute package path required")
        self.package = match[3]
        self.name = match[5] or self.package.split('/')[-1]

    @property
    def absolute(self):
        return self.package + ':' + self.name

    @property
    def package_path(self):
        assert self.package.startswith('//')
        rel_package = self.package[2:]
        return os.path.normpath(rel_package)

    @property
    def info_path(self):
        """Path to the msbuild info file for this label, relative to the bin path."""
        if self.repo:
            raise NotImplementedError("External repos")
        # TODO: absolute
        return os.path.join(self.package, self.name+'.msbuild')

class Struct:
    pass

class ProjectInfo:
    def __init__(self, label, info_dict):
        self.label = label
        #self.ws_path = info_dict['workspace_root']
        self.rule = Struct()
        self.rule.srcs = info_dict['files']['srcs']
        self.rule.hdrs = info_dict['files']['hdrs']
        self.output_files = info_dict['target']['files']
        self.guid = _generate_uuid_from_data(str(label))

        if self.output_files:
            output_file = self.output_files[0]
            self.output_file = Struct()
            self.output_file.path = os.path.dirname(output_file)
            self.output_file.basename = os.path.basename(output_file)
        else:
            self.output_file = None

class Configuration:
    def __init__(self, args):
        self.workspace_root = os.path.abspath('.')  # TODO
        self.output_path    = os.path.abspath(args.output)

        self.paths = Struct()
        self.paths.workspace_root = self.workspace_root
        self.paths.bin = os.path.join(self.workspace_root, 'bazel-bin')
        self.paths.out = os.path.join(self.workspace_root, 'bazel-out')

        self._setup_env()

        if args.target:
            self.targets = args.target
        else:
            # Query for all labels in the workspace.
            # For large codebases, this is going to take awhile!
            target_list = subprocess.check_output([BAZEL, 'query', '//...', '--output=label'])
            self.targets = [t.decode('utf-8').strip() for t in target_list.split(b'\n') if t]

        self.solution_name = args.solution or os.path.basename(os.getcwd())

    @property
    def bin_path(self):
        """Path to the bazel-bin directory of the current workspace."""
        return os.path.join(self.workspace_root, 'bazel-bin')

    def output_path_for_package(self, package):
        """Path to the output directory for files generated for the given package."""
        return os.path.join(self.output_path, package)

    def _setup_env(self):
        """Modifies the env vars of the process for bazel to run successfully."""
        # Tell MSYS2 not to rewrite absolute package paths in command line args.
        # Don't override a more aggressive setting.
        if os.environ.get('MSYS2_ARG_CONV_EXCL') != '*':
            os.environ['MSYS2_ARG_CONV_EXCL'] = '//'

    class RelativePathHelper:
        """Provides a set of paths relative to some starting path."""
        def __init__(self, orig_paths, relative_to):
            self.orig_paths = orig_paths
            self.relative_to = relative_to

        def __getattr__(self, name):
            orig = getattr(self.orig_paths, name)
            relpath = os.path.relpath(orig, self.relative_to)
            return os.path.normpath(relpath)

    def rel_paths(self, relative_to):
        return Configuration.RelativePathHelper(self.paths, relative_to)

def run_aspect(cfg):
    """Invokes bazel on our aspect to generate target info."""
    subprocess.check_call([
        BAZEL,
        'build',
        # TODO: inject aspect into project somehow
        '--aspects=bazel-msbuild/msbuild.bzl%msbuild_aspect',
        '--output_groups=msbuild_outputs'] + cfg.targets)

def read_info(cfg, target):
    """Reads the generated msbuild info file for the given target."""
    info_dict = json.load(open(os.path.join(cfg.bin_path, target.info_path)))
    return ProjectInfo(target, info_dict)

def _msb_cc_src(rel_ws_root, info, filename):
    return '<ClCompile Include="{}" />'.format(os.path.join(rel_ws_root, filename))

def _msb_cc_inc(rel_ws_root, info, filename):
    return '<ClInclude Include="{}" />'.format(os.path.join(rel_ws_root, filename))

def _msb_item_group(rel_ws_root, info, file_targets, func):
    if not file_targets:
        return ''
    return (
        '\n  <ItemGroup>' +
        '\n    '.join([''] + [func(rel_ws_root, info, f) for f in file_targets]) +
        '\n  </ItemGroup>'
    )

def _msb_files(cfg, info):
    output_dir = cfg.output_path_for_package(info.label.package)
    rel_ws_root = os.path.relpath(cfg.workspace_root, output_dir)
    return (
        _msb_item_group(rel_ws_root, info, info.rule.srcs, _msb_cc_src) +
        _msb_item_group(rel_ws_root, info, info.rule.hdrs, _msb_cc_inc))

def _sln_project(project):
    # This first UUID appears to be an identifier for Visual C++ packages?
    return (
        'Project("{type_guid}") = "{name}", "{package}\\{name}.vcxproj", "{guid}"\nEndProject'
        .format(guid=project.guid, type_guid=PROJECT_TYPE_GUID,
                name=project.label.name, package=project.label.package))

def _sln_projects(projects):
    return '\n'.join([_sln_project(project) for project in projects])

def _sln_project_cfgs(projects):
    lines = []
    for project in projects:
        lines.extend(s.format(guid=project.guid) for s in [
            '{guid}.Debug|x64.ActiveCfg = Debug|x64',
            '{guid}.Debug|x64.Build.0 = Debug|x64',
            '{guid}.Debug|x86.ActiveCfg = Debug|Win32',
            '{guid}.Debug|x86.Build.0 = Debug|Win32',
            '{guid}.Release|x64.ActiveCfg = Release|x64',
            '{guid}.Release|x64.Build.0 = Release|x64',
            '{guid}.Release|x86.ActiveCfg = Release|Win32',
            '{guid}.Release|x86.Build.0 = Release|Win32',
        ])
    return '\n\t\t'.join(lines)

def _generate_uuid_from_data(data):
    # We don't comply with any UUID standard, but we use 3 to advertise that it is a deterministic
    # hash of a name. I don't think Visual Studio will complain about the method used to create our
    # one-way hash.
    # TODO: Actually use more bits.
    hsh = abs(hash(data))
    part1 = hsh // (2**32)
    part2 = hsh % (2**32)
    return '{{{:08X}-0000-3000-A000-0000{:08X}}}'.format(part1, part2)

def _makedirs(path):
    """Ensures that the directories in path exist. Does nothing if they do."""
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def main(argv):
    parser = argparse.ArgumentParser(
        description="Generates Visual Studio project files from Bazel projects.")
    parser.add_argument("target", nargs='*',
                        help="Target to generate project for [default: all targets]")
    parser.add_argument("--output", "-o", type=str, default='.',
                        help="Output directory")
    parser.add_argument("--solution", "-n", type=str,
                        help="Solution name [default: current directory name]")
    args = parser.parse_args(argv[1:])

    cfg = Configuration(args)
    run_aspect(cfg)

    project_infos = []
    for target in cfg.targets:
        info = read_info(cfg, Label(target))
        with open(os.path.join(SCRIPT_DIR, 'templates', 'vcxproj.xml')) as f:
            template = f.read()
        project_dir = os.path.join(cfg.output_path, info.label.package)
        _makedirs(project_dir)
        with open(os.path.join(project_dir, info.label.name+'.vcxproj'), 'w') as out:
            content = template.format(
                cfg=cfg,
                target=info,
                #label=info.label,
                #package_path=os.path.normpath(info.label.package),
                outputs=';'.join([os.path.basename(f) for f in info.output_files]),
                file_groups=_msb_files(cfg, info),
                rel_paths=cfg.rel_paths(project_dir))
            out.write(content)
        project_infos.append(info)

    with open(os.path.join(SCRIPT_DIR, 'templates', 'solution.sln')) as f:
        template = f.read()
    sln_filename = os.path.join(cfg.output_path, cfg.solution_name+'.sln')
    with open(sln_filename, 'w') as out:
        content = template.format(
            projects=_sln_projects(project_infos),
            project_cfgs=_sln_project_cfgs(project_infos),
            guid=_generate_uuid_from_data(sln_filename))
        out.write(content)

if __name__ == '__main__':
    main(sys.argv)
