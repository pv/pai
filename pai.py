#!/usr/bin/env python
from __future__ import division

import sys, os, shutil, tempfile, re, UserDict
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import gc
import traceback
import optparse

IMAGE_EXTENSIONS = [ '.jpg', '.gif', '.png', '.tif', '.tiff', '.bmp' ]

##############################################################################
## Image list / recursive archive unpack

class ExtensionMap(UserDict.UserDict):
    def __init__(self, dictionary=None):
        UserDict.UserDict.__init__(self)
        if dictionary:
            self.data = dictionary
        
    def has_key(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return True
        return False

    def get(self, string, default=None):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return self.data[key]
        return default

    def __contains__(self, string):
        return self.has_key(string)

    def __getitem__(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return self.data[key]
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

def recursive_unpack(dirname, unpackers):
    """Unpack all archives in dirname and return a file list."""
    pathlist = [ os.path.abspath(dirname) ]
    files = []

    while len(pathlist) > 0:
        path = pathlist[0]
        del pathlist[0]

        if os.path.isdir(path):
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
    exitcode = os.spawnlp(os.P_WAIT,
                          "aunpack",
                          "aunpack", "-X", todir, archive)
    if exitcode != 0:
        raise ValueError("Archive unpack failed")

class RecursiveFileList:
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

    def __init__(self, filenames, extensionlist=None):
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
                                       RecursiveFileList.zip_extension_map)

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
## Image collection

class ImageCollection:
    def __init__(filenames):
        pass


##############################################################################
## ImageCache / ImageView

class ImageCache:
    def __init__(self, max_items=20):
        self.raw_pixbufs = {}
        self.scaled_pixbufs = {}
        self.filenames = []
        self.max_items = max_items
    
    def add(self, filename):
        if filename in self.filenames:
            return # noop
        
        # cleanup cache, if necessary
        if len(self.filenames) + 1 > self.max_items:
            del self.raw_pixbufs[self.filenames[0]]
            del self.scaled_pixbufs[self.filenames[0]]
            del self.filenames[0]
            gc.collect()

        # load image
        raw_pixbuf = gtk.gdk.pixbuf_new_from_file(filename)
        self.filenames.append(filename)
        self.raw_pixbufs[filename] = raw_pixbuf

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
                                             gtk.gdk.INTERP_BILINEAR)
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
        gtk.Container.__init__(self)
        self.xspacing = xspacing
        self.cache = cache

        self.pango_context = None
        self.pango_layout = None

        self.filenames = []

        style = self.get_style().copy()
        style.bg[gtk.STATE_NORMAL] = gtk.gdk.Color(0, 0, 0)
        self.set_style(style)

        self.text = u""

        self.unity_ratio = False

    def set_files(self, filenames):
        self.filenames = filenames
        self.queue_resize()

    def do_style_set(self, previous_style):
        if self.pango_layout:
            self.pango_layout.context_changed()
        if isinstance(previous_style, gtk.Style):
            gtk.Widget.do_style_set(self, previous_style)

    def do_direction_changed(self, direction):
        if self.pango_layout:
            self.pango_layout.context_changed()
        gtk.Widget.do_direction_changed(self, direction)

    def do_expose_event(self, event):
        if not self.window or not self.window.is_visible():
            return
        if not self.cache or not self.filenames:
            return False
        x, y, width, height = self.get_allocation()
        to_show = self.__get_files_to_show(self.filenames, width, height)

        # draw some text
        if not self.pango_context:
            self.pango_context = self.create_pango_context()
        if not self.pango_layout:
            self.pango_layout = self.create_pango_layout(self.text)
        self.pango_layout.set_text(self.text)
        self.window.draw_layout(self.get_style().white_gc,
                                1, 1, self.pango_layout)
        
        for xpos, ypos, pixbuf in to_show:
            self.blit_image(pixbuf, xpos, ypos, event.area)

    def preload(self, filenames):
        if not self.window or not self.window.is_visible():
            return
        if not isinstance(filenames, list):
            filenames = [filenames]
        x, y, width, height = self.get_allocation()
        self.__get_files_to_show(filenames, width, height)

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
## CollectionUI

class CollectionUI(ImageView):
    __gsignals__ = {
        'expose-event': 'override',
        }
    
    def __init__(self, sources, ncolumns=1, rtl=False):
        self.cache = ImageCache()
        ImageView.__init__(self, self.cache)

        self.sources = [ os.path.realpath(p) for p in sources
                         if os.path.exists(p) ]
        self.filelist = RecursiveFileList(self.sources, IMAGE_EXTENSIONS)
        self.pos = 0
        self.rtl = rtl
        self.ncolumns = ncolumns

        self.connect("map-event", self.__map_event)

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
        self.queue_draw()

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

    def __preload_callback(self):
        self.preload(self.__get_preload_files())
        return False

    def do_expose_event(self, event):
        ImageView.do_expose_event(self, event)
        gobject.idle_add(self.__preload_callback)

    def __update_position(self):
        files = self.__get_show_files()
        filenames = [ self.filelist.to_filename(f) for f in files ]
        self.text = u"%d / %d: %s" % (
            self.pos+1, len(self.filelist),
            unicode(', '.join(filenames), "latin-1"))
        self.set_files(files)
        gobject.idle_add(self.__preload_callback)

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
        self.close

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

class Bookmarks:
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

class PaiUI:
    def __init__(self, sources, config, rtl=False, ncolumns=2):
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)

        self.config = config

        self.collection = CollectionUI(sources, ncolumns=ncolumns, rtl=rtl)
        self.collection.set_size_request(300, 200)

        self.bookmarks = Bookmarks(self.collection.sources, self.config)
        self.collection.goto(self.bookmarks[0])

        self.window.connect("destroy", self.destroy_event)
        self.window.connect("key-press-event", self.key_press_event)

        self.window.add(self.collection)
        self.window.show_all()
        self.window.fullscreen()

        self.fullscreen = True
        
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

    def close(self):
        self.bookmarks[0] = self.collection.pos
        self.collection.close()
        gtk.main_quit()

    def destroy_event(self, widget):
        self.close()
        
    def main(self):
        gtk.main()

    def __del__(self):
        self.close()

if __name__ == "__main__":
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
    ui = PaiUI(args, config, ncolumns=options.ncolumns, rtl=options.rtl)
    ui.main()
    config.save(config_fn)

    raise SystemExit(0)
