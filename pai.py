#!/usr/bin/env python
"""
===
pai
===

-------------------------
Picture archive inspector
-------------------------

:Author: Pauli Virtanen <pav@iki.fi>
"""
from __future__ import division

import sys, os, shutil, tempfile, re, random, time, traceback, copy
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import gc
import traceback
import optparse

import threading, Queue

IMAGE_EXTENSIONS = [ '.jpg', '.gif', '.png', '.tif', '.tiff', '.bmp' ]

##############################################################################
## GUI threading
##############################################################################
#
# Every time you call a non-threadsafe GTK function (=most of them),
# be sure that either
# 1. The function where you make the call has @assert_gui_thread, OR,
# 2. You make the call via run_in_gui_thread
#
# Blocks to run in gui thread sometime later (as a shorthand) can be
# specified via
#
# >>> @run_in_gui_thread
# ... def _():
# ...     stuff_to_do
#
# But remember how lexical binding works though,
#
# >>> item = True
# >>> def foo():
# ...     assert item == True
# >>> item = False
# >>> foo()
# Traceback (most recent call last):
#   ...
# AssertionError
#

def run_in_gui_thread(func, *a, **kw):
    """Run the function in the GUI thread, next time when the GUI is idle."""
    def timer():
        func(*a, **kw)
        return False
    gobject.idle_add(timer)
    return None

def assert_gui_thread(func):
    """Assert that this function is ran in the GUI thread. [decorator]"""
    if not __debug__: return func
    def _wrapper(*a, **kw):
        assert threading.currentThread() == threading.enumerate()[0], \
               (func, threading.currentThread(), threading.enumerate())
        return func(*a, **kw)
    return _wrapper


##############################################################################
## Image list / recursive archive unpack
##############################################################################

class ExtensionMap(dict):
    def __init__(self, dictionary=None):
        dict.__init__(self)
        self.update(dictionary)
        
    def has_key(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return True
        return False

    def get(self, string, default=None):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return dict.__getitem__(self, key)
        return default

    def __contains__(self, string):
        return self.has_key(string)

    def __getitem__(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return dict.__getitem__(self, key)
        raise KeyError(string)

def numeric_file_sort(filelist):
    """Return the given list, sorted by file name, not heeding
       leading zeroes in numbers."""
    
    def sort_key(filename):
        key = ""
        lastend = 0
        for match in re.finditer("\d+", filename):
            key += filename[lastend:match.start()]
            lastend = match.end()
            key += "%08d" % (int(match.group()))
        return key

    lst = list(filelist)
    lst.sort(key=sort_key)
    return lst

def recursive_unpack(dirname, unpackers, progress_queue=None):
    """Unpack all archives in dirname and return a file list."""
    pathlist = [ os.path.abspath(dirname) ]
    files = []

    while len(pathlist) > 0:
        path = pathlist[0]
        del pathlist[0]

        if os.path.isdir(path):
            if progress_queue: progress_queue.put(os.path.basename(path))
            if os.path.islink(path):
                items = os.listdir(path)
                target = os.path.join(os.path.join(path, os.path.pardir),
                                      os.readlink(path))
                os.unlink(path)
                os.mkdir(path)
                for item in items:
                    os.symlink(os.path.join(target, item),
                               os.path.join(path, item))
                
            pathlist += numeric_file_sort([os.path.join(path, i)
                                           for i in os.listdir(path)])
        elif os.path.isfile(path) and path in unpackers:
            if progress_queue: progress_queue.put(os.path.basename(path))
            unpacker = unpackers[path]
            root, ext = os.path.splitext(path)
            tmpname = tempfile.mktemp(ext, '', os.path.dirname(path))
            shutil.move(path, tmpname)
            os.mkdir(path)
            try:
                unpacker(tmpname, path)
                os.unlink(tmpname)
                pathlist.append(path)
            except:
                # unpack failed for some reason... do not do it then
                os.rmdir(path)
                shutil.move(tmpname, path)
        else:
            files.append(path)

    return files

def unpack_atool(archive, todir):
    """Unpack an archive using ``aunpack`` from ``atool``."""
    exitcode = os.spawnlp(os.P_WAIT,
                          "aunpack",
                          "aunpack", "-X", todir, archive)
    if exitcode != 0:
        raise ValueError("Archive unpack failed")

class RecursiveFileList(object):
    """Get a list of files in given sources, including contents of archives,
    which will be recursively unpacked."""
    
    zip_extension_map = ExtensionMap({
        '.zip': unpack_atool,
        '.tar': unpack_atool,
        '.tar.gz': unpack_atool,
        '.tar.bz2': unpack_atool,
        '.tbz': unpack_atool,
        '.tb2': unpack_atool, 
        '.tgz': unpack_atool,
        '.rar': unpack_atool,
        '.cbr': unpack_atool,
        })

    def __init__(self, filenames, extensionlist=None, progress_queue=None):
        if not isinstance(filenames, list):
            filenames = [filenames]
        
        self._cache_dir = tempfile.mkdtemp()

        # Link seed files
        self.sources = []
        for filename in filenames:
            try:
                absfilename = os.path.abspath(filename)
                targetname = os.path.join(self._cache_dir,
                                          os.path.basename(absfilename))
                os.symlink(absfilename, targetname)
                self.sources.append([absfilename, targetname])
            except OSError:
                traceback.print_exc()

        # Recursive unpack
        self._files = recursive_unpack(self._cache_dir,
                                       RecursiveFileList.zip_extension_map,
                                       progress_queue)

        # List
        self._files = [i for i in self._files
                       if os.path.exists(i) ]
        
        if extensionlist:
            self._files = [i for i in self._files
                           if i[-4:].lower() in extensionlist]

    def close(self):
        import shutil
        if self._cache_dir:
            shutil.rmtree(self._cache_dir)
        self._cache_dir = None
        self._files = None

    def __str__(self):
        return str(self._files)

    def __del__(self):
        self.close()

    def filename(self, i):
        return self.to_filename(self._files[i])

    def to_filename(self, fn):
        for non_cached, cached in self.sources:
            if os.path.commonprefix([fn, cached]) == cached:
                fn = non_cached + fn[len(cached):]
                return fn
        return fn

    def __getitem__(self, i):
        return self._files[i]

    def __len__(self):
        return len(self._files)


##############################################################################
## ImageCache / ImageView
##############################################################################

class ImageCache(object):
    def __init__(self, max_items=10):
        self.raw_pixbufs = {}
        self.scaled_pixbufs = {}
        self.filenames = []
        self.max_items = max_items
        self.interpolation = gtk.gdk.INTERP_BILINEAR
    
    def add(self, filename):
        if filename in self.filenames:
            return # noop
        
        # cleanup cache, if necessary
        if len(self.filenames) + 1 > self.max_items:
            try:
                del self.raw_pixbufs[self.filenames[0]]
            except KeyError:
                pass
            try:
                del self.scaled_pixbufs[self.filenames[0]]
            except KeyError:
                pass
            del self.filenames[0]
            gc.collect()

        # load image
        raw_pixbuf = gtk.gdk.pixbuf_new_from_file(filename)
        self.filenames.append(filename)
        self.raw_pixbufs[filename] = raw_pixbuf

    def set_interpolation(self, interpolation):
        self.scaled_pixbufs = {}
        self.interpolation = interpolation

    def get(self, filename):
        self.add(filename)
        return self.raw_pixbufs[filename]

    def get_scaled(self, filename, width, height):
        self.add(filename)
        try:
            pixbuf = self.scaled_pixbufs[filename]
            if pixbuf.get_width() != width or pixbuf.get_height() != height:
                raise KeyError(filename)
            return pixbuf
        except KeyError:
            pixbuf = self.raw_pixbufs[filename]
            if width != pixbuf.get_width() or height != pixbuf.get_height():
                pixbuf = pixbuf.scale_simple(width, height,
                                             self.interpolation)
            self.scaled_pixbufs[filename] = pixbuf
            gc.collect()
            return pixbuf

class ImageView(gtk.DrawingArea):
    __gsignals__ = {
        'expose-event': 'override',
        'style-set': 'override',
        'direction-changed': 'override',
        }

    def __init__(self, cache, xspacing=0):
        self.xspacing = xspacing
        self.cache = cache

        self.pango_context = None
        self.pango_layout = None

        self.filenames = []

        self.text = u""

        self.unity_ratio = False

        @run_in_gui_thread
        def _():
            gtk.DrawingArea.__init__(self)

            style = self.get_style().copy()
            style.bg[gtk.STATE_NORMAL] = gtk.gdk.Color(0, 0, 0)
            self.set_style(style)

    def set_files(self, filenames):
        self.filenames = filenames
        run_in_gui_thread(self.queue_resize)

    @assert_gui_thread
    def do_style_set(self, previous_style):
        if self.pango_layout:
            self.pango_layout.context_changed()
        if isinstance(previous_style, gtk.Style):
            gtk.Widget.do_style_set(self, previous_style)

    @assert_gui_thread
    def do_direction_changed(self, direction):
        if self.pango_layout:
            self.pango_layout.context_changed()
        gtk.Widget.do_direction_changed(self, direction)

    @assert_gui_thread
    def do_expose_event(self, event):
        if not self.window or not self.window.is_visible():
            return
        if not self.cache or not self.filenames:
            return False
        x, y, width, height = self.get_allocation()

        # draw some text
        if not self.pango_context:
            self.pango_context = self.create_pango_context()
        if not self.pango_layout:
            self.pango_layout = self.create_pango_layout(self.text)
        self.pango_layout.set_text(self.text)
        text_size = self.pango_layout.get_pixel_size()
        height -= text_size[1]
        self.window.draw_layout(self.get_style().white_gc, 0, 0,
                                self.pango_layout,
                                background=gtk.gdk.Color(0,0,0),
                                foreground=gtk.gdk.Color(65535,65535,65535))
 
        # render image
        to_show = self.__get_files_to_show(self.filenames, width, height)
        for xpos, ypos, pixbuf in to_show:
            self.blit_image(pixbuf, xpos, text_size[1] + ypos, event.area)

    @assert_gui_thread
    def preload(self, filenames):
        if not self.window or not self.window.is_visible():
            return
        if not isinstance(filenames, list):
            filenames = [filenames]
        x, y, width, height = self.get_allocation()
        self.__get_files_to_show(filenames, width, height)

    @assert_gui_thread
    def __get_files_to_show(self, files, win_width, win_height):
        xpos = []
        ypos = []
        pixbufs = []

        total_width = 0
        total_height = 0

        for i in range(len(files)):
            raw_pixbuf = self.cache.get(files[i])
            total_width += raw_pixbuf.get_width()
            total_height = max(raw_pixbuf.get_height(), total_height)

        available_width = win_width - self.xspacing*(len(files) - 1)
        available_height = win_height

        ratio = min(available_width / total_width,
                    available_height / total_height)

        if self.unity_ratio:
            ratio = 1

        for i in range(len(files)):
            raw_pixbuf = self.cache.get(files[i])
            img_width = int(raw_pixbuf.get_width() * ratio)
            img_height = int(raw_pixbuf.get_height() * ratio)

            pixbuf = self.cache.get_scaled(files[i], img_width, img_height)
            if not xpos:
                xpos.append(img_width + self.xspacing)
            elif i == len(files) - 1:
                xpos.append(xpos[-1] + img_width)
            else:
                xpos.append(xpos[-1] + img_width + self.xspacing)
            ypos.append(.5*(win_height - img_height))
            pixbufs.append(pixbuf)

        xpos.insert(0, 0)
        offset = .5*(win_width - xpos[-1] - xpos[0])
        xpos = [x + offset for x in xpos]
        xpos.pop()
        
        return zip(xpos, ypos, pixbufs)

    @assert_gui_thread
    def blit_image(self, pixbuf, dst_x, dst_y, clip_rect):
        pixbuf_area = gtk.gdk.Rectangle()
        pixbuf_area.width = pixbuf.get_width()
        pixbuf_area.height = pixbuf.get_height()

        xoff = int(dst_x)
        yoff = int(dst_y)

        clip_rect.x -= xoff
        clip_rect.y -= yoff
        area = pixbuf_area.intersect(clip_rect)

        if area.width > 0 and area.height > 0:
            self.window.draw_pixbuf(self.get_style().black_gc,
                                    pixbuf,
                                    area.x, area.y,
                                    int(area.x+xoff), int(area.y+yoff),
                                    area.width, area.height)
        return True


##############################################################################
## UI
##############################################################################

class CollectionUI(ImageView):
    __gsignals__ = {
        'expose-event': 'override',
        }
    
    def __init__(self, sources, ncolumns=1, rtl=False, progress_dlg=None):
        self.cache = ImageCache()
        ImageView.__init__(self, self.cache)

        self.sources = [ os.path.realpath(p) for p in sources
                         if os.path.exists(p) ]
        self.filelist = RecursiveFileList(self.sources, IMAGE_EXTENSIONS,
                                          progress_dlg.queue)
        progress_dlg.close()
        self.pos = 0
        self.rtl = rtl
        self.ncolumns = ncolumns

        run_in_gui_thread(self.connect, "map-event", self.__map_event)

    def next(self, count=1):
        self.pos += count
        self.__limit_position()
        self.__update_position()

    def previous(self, count=1):
        self.pos -= count
        self.__limit_position()
        self.__update_position()

    def next_screen(self, count=1):
        self.pos += self.ncolumns*count
        self.__limit_position()
        self.__update_position()

    def previous_screen(self, count=1):
        self.pos -= self.ncolumns*count
        self.__limit_position()
        self.__update_position()

    def goto(self, i):
        self.pos = i
        self.__limit_position()
        self.__update_position()

    def first(self):
        self.pos = 0
        self.__update_position()

    def last(self):
        self.pos = len(self.filelist) - self.ncolumns
        self.__update_position()

    def update_view(self):
        self.__limit_position()
        self.__update_position()
        run_in_gui_thread(self.queue_draw)

    def set_interpolation(self, interpolation):
        self.cache.set_interpolation(interpolation)

    def get_interpolation(self):
        return self.cache.interpolation

    @assert_gui_thread
    def __map_event(self, widget, event):
        self.__update_position()
        self.preload(self.__get_preload_files())

    def __limit_position(self):
        if self.pos > len(self.filelist) - self.ncolumns:
            self.pos = len(self.filelist) - self.ncolumns
        if self.pos < 0:
            self.pos = 0

    def preload(self, files):
        preload_files = self.__get_preload_files()
        for i in range(0, len(preload_files), self.ncolumns):
            j = i + self.ncolumns
            if j >= len(preload_files):
                preload_files += [preload_files[-1]] * self.ncolumns
            ImageView.preload(self, preload_files[i:j])

    @assert_gui_thread
    def do_expose_event(self, event):
        ImageView.do_expose_event(self, event)
        run_in_gui_thread(self.preload, self.__get_preload_files())

    def __update_position(self):
        files = self.__get_show_files()
        filenames = [ self.filelist.to_filename(f) for f in files ]
        filenames = [ os.path.join(os.path.basename(os.path.dirname(f)),
                                   os.path.basename(f))
                      for f in filenames ]
        self.text = u"%d / %d: %s" % (
            self.pos+1, len(self.filelist),
            unicode(', '.join(filenames), "latin-1"))
        self.set_files(files)

        run_in_gui_thread(self.preload, self.__get_preload_files())

    def __get_show_files(self):
        endpos = self.pos + self.ncolumns
        if endpos > len(self.filelist):
            endpos = len(self.filelist)

        filelist = self.filelist[self.pos:endpos]

        if self.rtl:
            filelist.reverse()

        return filelist

    def __get_preload_files(self):
        files = []
        for i in range(-self.ncolumns, 2*self.ncolumns+1, 1):
            if 0 <= self.pos + i < len(self.filelist):
                files.append(self.filelist[self.pos + i])
        return files

    def close(self):
        self.filelist.close()

    def __del__(self):
        self.close()

class Config(dict):
    def load(self, filename):
        f = open(filename, "r")
        for line in f:
            try:
                key, value = line.split("\t", 1)
                self[key] = value
            except:
                pass
            

    def save(self, filename):
        f = open(filename, "w")
        for key, value in self.items():
            if key:
                f.write("%s\t%s\n" % (key, str(value)))

class Bookmarks(object):
    def __init__(self, sources, config):
        if not isinstance(sources, list):
            sources = [sources]

        self.config = config
        self.configkey = ':'.join(sources).replace("\t", "_")

        self.values = []

        try:
            if self.configkey in self.config:
                self.values = map(int, self.config[self.configkey].split("\t"))
        except:
            self.values = []
            
        while len(self.values) < 10:
            self.values.append(0)
        if len(self.values) > 10:
            self.values = self.values[0:10]

    def __setitem__(self, i, value):
        self.values[i] = int(value)
        self.config[self.configkey] = '\t'.join(map(str, self.values))

    def __getitem__(self, i):
        return self.values[i]

class PaiUI(object):
    def __init__(self, sources, config, rtl=False, ncolumns=2,
                 progress_dlg=None):

        self.config = config

        self.collection = CollectionUI(sources, ncolumns=ncolumns, rtl=rtl,
                                       progress_dlg=progress_dlg)

        self.bookmarks = Bookmarks(self.collection.sources, self.config)
        self.collection.goto(self.bookmarks[0])

        self.fullscreen = False#True
        
        @run_in_gui_thread
        def _():
            self.collection.set_size_request(300, 200)

            self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
            self.window.connect("destroy", self.destroy_event)
            self.window.connect("key-press-event", self.key_press_event)
            self.window.add(self.collection)


    @assert_gui_thread
    def show(self):
        self.window.show_all()
        if self.fullscreen:
            self.window.fullscreen()
        
    @assert_gui_thread
    def key_press_event(self, widget, event):
        if event.keyval == gtk.keysyms.q:
            self.close()
        elif event.keyval == gtk.keysyms.space:
            self.collection.next_screen()
        elif event.keyval == gtk.keysyms.b:
            self.collection.previous_screen()
        elif event.keyval == gtk.keysyms.Home:
            self.collection.first()
        elif event.keyval == gtk.keysyms.Left:
            if not self.collection.rtl:
                self.collection.previous()
            else:
                self.collection.next()
        elif event.keyval == gtk.keysyms.Right:
            if not self.collection.rtl:
                self.collection.next()
            else:
                self.collection.previous()
        elif event.keyval == gtk.keysyms.End:
            self.collection.last()
        elif event.keyval == gtk.keysyms.Prior:
            self.collection.previous_screen(10)
        elif event.keyval == gtk.keysyms.Next:
            self.collection.next_screen(10)
        elif event.keyval == gtk.keysyms.r:
            self.collection.rtl = not self.collection.rtl
            self.collection.update_view()
        elif event.keyval == gtk.keysyms.d:
            if self.collection.ncolumns > 1:
                self.collection.ncolumns = 1
            else:
                self.collection.ncolumns = 2
            self.collection.update_view()
        elif event.keyval == gtk.keysyms.u:
            self.collection.unity_ratio = not self.collection.unity_ratio
            self.collection.update_view()
        elif event.keyval == gtk.keysyms.f:
            if not self.fullscreen:
                self.window.fullscreen()
                self.fullscreen = True
            else:
                self.window.unfullscreen()
                self.fullscreen = False
        elif event.keyval == gtk.keysyms.i:
            if self.collection.get_interpolation() == gtk.gdk.INTERP_BILINEAR:
                self.collection.set_interpolation(gtk.gdk.INTERP_HYPER)
            elif self.collection.get_interpolation() == gtk.gdk.INTERP_HYPER:
                self.collection.set_interpolation(gtk.gdk.INTERP_NEAREST)
            else:
                self.collection.set_interpolation(gtk.gdk.INTERP_BILINEAR)
            self.collection.update_view()

    @assert_gui_thread
    def close(self):
        self.bookmarks[0] = self.collection.pos
        self.collection.close()
        gtk.main_quit()

    @assert_gui_thread
    def destroy_event(self, widget):
        self.close()


##############################################################################
## Progress dialog
##############################################################################

class ProgressDialog(object):
    """A dialog box that reports progress.

    >>> dlg = ProgressDialog()
    >>> dlg.set_progress(percentage=50, text="Foo")
    >>> dlg.close()
    """
    def __init__(self, title=""):
        @run_in_gui_thread
        def _():
            self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
            self.window.set_title(title)

            vbox = gtk.VBox(False, 2)
            vbox.set_border_width(10)
            self.window.add(vbox)
            vbox.show()

            self.label = gtk.Label()
            vbox.pack_start(self.label)
            self.label.show()

            self.bar = gtk.ProgressBar()
            vbox.pack_end(self.bar)
            self.bar.show()

            self.window.show()

        self.queue = Queue.Queue()
        self.__listener = threading.Thread(target=self.__listener)
        self.__listener.start()

    def __listener(self):
        while True:
            item = copy.copy(self.queue.get())
            if isinstance(item, str):
                run_in_gui_thread(self.bar.set_text, item)
                run_in_gui_thread(self.label.set_text, item)
                run_in_gui_thread(self.bar.pulse)
            elif item == None:
                run_in_gui_thread(self.window.destroy)
                return

    @assert_gui_thread
    def __getattr__(self, name):
        return getattr(self.bar, name)

    def close(self):
        self.queue.put(None)


##############################################################################
## Main
##############################################################################

def start(args, options, config):
    progress = ProgressDialog("Starting PAI...")

    ui = PaiUI(args, config, ncolumns=options.ncolumns, rtl=options.rtl,
               progress_dlg=progress)

    run_in_gui_thread(ui.show)

def main():
    parser = optparse.OptionParser(usage="%prog [options] images-or-something")
    parser.add_option("-r", "--rtl", action="store_true", dest="rtl",
                      help="show images in right-to-left order", default=True)
    parser.add_option("-l", "--ltr", action="store_false", dest="rtl",
                      help="show images in left-to-right order")
    parser.add_option("-c", "--columns", type="int", dest="ncolumns",
                      help="show images in N columns", default=2)
    options, args = parser.parse_args()

    if len(args) < 1:
        parser.error("no image sources given")

    if options.ncolumns > 4 or options.ncolumns < 1:
        parser.error("invalid number of columns given")
        
    config_fn = "%s/.pairc" % os.environ["HOME"]
    config = Config()
    if os.path.exists(config_fn):
        config.load(config_fn)

    gtk.gdk.threads_init()

    threading.Thread(target=start, args=(args, options, config,)).start()

    gtk.main()

    config.save(config_fn)

    sys.exit(0)

if __name__ == "__main__": main()
