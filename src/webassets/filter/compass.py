"""
Generally speaking, compass provides a command line util that is used
  a) as a management script (like django-admin.py) doing for example
    setup work, adding plugins to a project etc), and
  b) can compile the sass source files into CSS.

While generally project-based, starting with 0.10, compass supposedly
supports compiling individual files, which is what we are using for
implementing this filter. Supposedly, because there are numerous issues
that require working around. See the comments in the actual filter code
for the full story on all the hoops be have to jump through.

An alternative option would be to use Sass to compile. Compass essentially
adds two things on top of sass: A bunch of CSS frameworks, ported to Sass,
and available for including. And various ruby helpers that these frameworks
and custom Sass files can use. Apparently there is supposed to be a way
to compile a compass project through sass, but so far, I haven't got it
to work. The syntax is supposed to be one of:

    $ sass -r compass `compass imports` FILE
    $ sass --compass FILE

See:
    http://groups.google.com/group/compass-users/browse_thread/thread/a476dfcd2b47653e
    http://groups.google.com/group/compass-users/browse_thread/thread/072bd8b51bec5f7c
    http://groups.google.com/group/compass-users/browse_thread/thread/daf55acda03656d1
"""

import os
from os import path
import tempfile
import shutil
import subprocess
from webassets import six

from webassets.exceptions import FilterError
from webassets.filter import Filter, option


__all__ = ('Compass',)


class CompassConfig(dict):
    """A trivial dict wrapper that can generate a Compass config file."""

    def to_string(self):
        def string_rep(val):
            """ Determine the correct string rep for the config file """
            if isinstance(val, bool):
                # True -> true and False -> false
                return str(val).lower()
            elif isinstance(val, six.string_types) and val.startswith(':'):
                # ruby symbols, like :nested, used for "output_style"
                return str(val)
            elif isinstance(val, dict):
                # ruby hashes, for "sass_options" for example
                return '{%s}' % ', '.join("'%s' => '%s'" % i for i in val.items())
            elif isinstance(val, tuple):
                val = list(val)
            elif isinstance(val, six.text_type) and not six.PY3:
                # remove unicode indicator in python2 unicode string
                return repr(val.encode('utf-8'))
            # works fine with strings and lists
            return repr(val)
        return '\n'.join(['%s = %s' % (k, string_rep(v)) for k, v in self.items()])


class Compass(Filter):
    """Converts `Compass <http://compass-style.org/>`_ .sass files to
    CSS.

    Requires at least version 0.10.

    To compile a standard Compass project, you only need to have
    to compile your main ``screen.sass``, ``print.sass`` and ``ie.sass``
    files. All the partials that you include will be handled by Compass.

    If you want to combine the filter with other CSS filters, make
    sure this one runs first.

    Supported configuration options:

    COMPASS_BIN
        The path to the Compass binary. If not set, the filter will
        try to run ``compass`` as if it's in the system path.

    COMPASS_PLUGINS
        Compass plugins to use. This is equivalent to the ``--require``
        command line option of the Compass. and expects a Python list
        object of Ruby libraries to load.

    COMPASS_CONFIG
        An optional dictionary of Compass `configuration options
        <http://compass-style.org/help/tutorials/configuration-reference/>`_.
        The values are emitted as strings, and paths are relative to the
        Environment's ``directory`` by default; include a ``project_path``
        entry to override this.
    """

    name = 'compass'
    max_debug_level = None
    options = {
        'compass': ('binary', 'COMPASS_BIN'),
        'plugins': option('COMPASS_PLUGINS', type=list),
        'config': 'COMPASS_CONFIG',
    }

    def open(self, out, source_path, **kw):
        """Compass currently doesn't take data from stdin, and doesn't allow
        us accessing the result from stdout either.

        Also, there's a bunch of other issues we need to work around:

         - compass doesn't support given an explict output file, only a
           "--css-dir" output directory.

           We have to "guess" the filename that will be created in that
           directory.

         - The output filename used is based on the input filename, and
           simply cutting of the length of the "sass_dir" (and changing
           the file extension). That is, compass expects the input
           filename to always be inside the "sass_dir" (which defaults to
           ./src), and if this is not the case, the output filename will
           be gibberish (missing characters in front). See:
           https://github.com/chriseppstein/compass/issues/304

           We fix this by setting the proper --sass-dir option.

         - Compass insists on creating a .sass-cache folder in the
           current working directory, and unlike the sass executable,
           there doesn't seem to be a way to disable it.

           The workaround is to set the working directory to our temp
           directory, so that the cache folder will be deleted at the end.
        """

        tempout = tempfile.mkdtemp()
        # Temporarily move to "tempout", so .sass-cache will be created there
        old_wd = os.getcwd()
        os.chdir(tempout)
        try:
            # Make sure to use normpath() to not cause trouble with
            # compass' simplistic path handling, where it just assumes
            # source_path is within sassdir, and cuts off the length of
            # sassdir from the input file.
            sassdir = path.normpath(path.dirname(source_path))
            source_path = path.normpath(source_path)

            # Compass offers some helpers like image-url(), which need
            # information about the urls under which media files will be
            # available. This is hard for two reasons: First, the options in
            # question aren't supported on the command line, so we need to write
            # a temporary config file. Secondly, the assume a defined and
            # separate directories for "images", "stylesheets" etc., something
            # webassets knows nothing of: we don't support the user defining
            # something such directories. Because we traditionally had this
            # filter point all type-specific directories to the root media
            # directory, we will define the paths to match this. In other
            # words, in Compass, both inline-image("img/test.png) and
            # image-url("img/test.png") will find the same file, and assume it
            # to be {env.directory}/img/test.png.
            # However, this partly negates the purpose of an utility like
            # image-url() in the first place - you not having to hard code
            # the location of your images. So we allow direct modification of
            # the configuration file via the COMPASS_CONFIG setting (see
            # tickets #36 and #125).
            #
            # Note that is also the --relative-assets option, which we can't
            # use because it calculates an actual relative path between the
            # image and the css output file, the latter being in a temporary
            # directory in our case.
            config = CompassConfig(
                project_path=self.env.directory,
                http_path=self.env.url,
                http_images_dir='',
                http_stylesheets_dir='',
                http_fonts_dir='',
                http_javascripts_dir='',
                images_dir='',
                output_style=':expanded',
            )
            # Update with the custom config dictionary, if any.
            if self.config:
                config.update(self.config)
            config_file = path.join(tempout, '.config.rb')
            f = open(config_file, 'w')
            try:
                f.write(config.to_string())
                f.flush()
            finally:
                f.close()

            command = [self.compass or 'compass', 'compile']
            for plugin in self.plugins or []:
                command.extend(('--require', plugin))
            command.extend(['--sass-dir', sassdir,
                            '--css-dir', tempout,
                            '--config', config_file,
                            '--quiet',
                            '--boring',
                            source_path])

            proc = subprocess.Popen(command,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    # shell: necessary on windows to execute
                                    # ruby files, but doesn't work on linux.
                                    shell=(os.name == 'nt'))
            stdout, stderr = proc.communicate()

            # compass seems to always write a utf8 header? to stderr, so
            # make sure to not fail just because there's something there.
            if proc.returncode != 0:
                raise FilterError(('compass: subprocess had error: stderr=%s, '+
                                   'stdout=%s, returncode=%s') % (
                                                stderr, stdout, proc.returncode))


            guessed_outputfile = \
                path.join(tempout, path.splitext(path.basename(source_path))[0])
            f = open("%s.css" % guessed_outputfile)
            try:
                out.write(f.read())
            finally:
                f.close()
        finally:
            # Restore previous working dir
            os.chdir(old_wd)
            # Clean up the temp dir
            shutil.rmtree(tempout)
