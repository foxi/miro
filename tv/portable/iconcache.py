import item
import os
import threading
import httpclient
from fasttypes import LinkedList
from eventloop import asIdle, addIdle, addTimeout
from download_utils import nextFreeFilename
from util import unicodify
from platformutils import unicodeToFilename, makeURLSafe
import config
import prefs
import time
import views
import random

RUNNING_MAX = 3

iconExtractors = list()
def registerIconExtractor(extractor):
    iconExtractors.append(extractor)
    
def clearOrphans():
    knownIcons = set()
    for item in views.items:
        if item.iconCache and item.iconCache.filename:
            knownIcons.add(os.path.normcase(item.iconCache.filename))
    for feed in views.feeds:
        if feed.iconCache and feed.iconCache.filename:
            knownIcons.add(os.path.normcase(feed.iconCache.filename))
    cachedir = config.get(prefs.ICON_CACHE_DIRECTORY)
    if os.path.isdir(cachedir):
        existingFiles = [os.path.normcase(os.path.join(cachedir, f)) 
                for f in os.listdir(cachedir)]
        for file in existingFiles:
            if (os.path.exists(file) and os.path.basename(file)[0] != '.' and 
                    not file in knownIcons):
                try:
                    os.remove (file)
                except OSError:
                    pass
    

class IconCacheUpdater:
    def __init__ (self):
        self.idle = LinkedList()
        self.vital = LinkedList()
        self.runningCount = 0
        self.inShutdown = False

    @asIdle
    def requestUpdate (self, item, is_vital = False):
        if is_vital:
            item.dbItem.confirmDBThread()
            if item.filename and os.access (item.filename, os.R_OK):
                is_vital = False
        if self.runningCount < RUNNING_MAX:
            addIdle (item.requestIcon, "Icon Request")
            self.runningCount += 1
        else:
            if is_vital:
                self.vital.prepend(item)
            else:
                self.idle.prepend(item)

    def updateFinished (self):
        if self.inShutdown:
            self.runningCount -= 1
            return

        if len (self.vital) > 0:
            item = self.vital.pop()
        elif len (self.idle) > 0:
            item = self.idle.pop()
        else:
            self.runningCount -= 1
            return
        
        addIdle (item.requestIcon, "Icon Request")

    @asIdle
    def clearVital (self):
        self.vital = LinkedList()

    @asIdle
    def shutdown (self):
        self.inShutdown = True

iconCacheUpdater = IconCacheUpdater()
class IconCache:
    def __init__ (self, dbItem, is_vital = False):
        self.etag = None
        self.modified = None
        self.filename = None
        self.url = None

        self.updated = False
        self.updating = False
        self.needsUpdate = False
        self.dbItem = dbItem
        self.removed = False

        self.requestUpdate (is_vital=is_vital)

    def remove (self):
        self.removed = True
        try:
            if self.filename:
                os.remove (self.filename)
        except:
            pass


    def errorCallback(self, url, error = None):
        self.dbItem.confirmDBThread()

        if self.removed:
            iconCacheUpdater.updateFinished()
            return

        # Don't clear the cache on an error.
        if self.url != url:
            self.url = url
            self.etag = None
            self.modified = None
            self.dbItem.signalChange()
        self.updating = False
        if self.needsUpdate:
            self.needsUpdate = False
            self.requestUpdate()
        elif error is not None:
            addTimeout(3600,self.requestUpdate, "Thumbnail request for %s" % url)
        else:
            self.updated = True
        iconCacheUpdater.updateFinished ()

    def updateIconCache (self, url, info):
        self.dbItem.confirmDBThread()

        if self.removed:
            iconCacheUpdater.updateFinished()
            return

        needsSave = False
        needsChange = False

        if info == None or (info['status'] != 304 and info['status'] != 200):
            self.errorCallback(url)
            return
        try:
            # Our cache is good.  Hooray!
            if (info['status'] == 304):
                self.updated = True
                return

            needsChange = True

            # We have to update it, and if we can't write to the file, we
            # should pick a new filename.
            if (self.filename and not os.access (self.filename, os.R_OK | os.W_OK)):
                self.filename = None
                seedsSave = True

            cachedir = config.get(prefs.ICON_CACHE_DIRECTORY)
            try:
                os.makedirs (cachedir)
            except:
                pass

            try:
                # Write to a temp file.
                if (self.filename):
                    tmp_filename = self.filename + ".part"
                else:
                    tmp_filename = os.path.join(cachedir, info["filename"]) + ".part"

                tmp_filename = nextFreeFilename (tmp_filename)
                output = file (tmp_filename, 'wb')
                output.write(info["body"])
                output.close()
            except IOError:
                try:
                    os.remove (tmp_filename)
                except:
                    pass
                return

            if (self.filename == None):
                # Add a random unique id
                parts = unicodify(info["filename"]).split('.')
                uid = u"%08d" % (random.randint(0,99999999),)
                if len(parts) == 1:
                    parts.append(uid)
                else:
                    parts[-1:-1] = [uid]
                self.filename = u'.'.join(parts)
                self.filename = unicodeToFilename(self.filename, cachedir)
                self.filename = os.path.join(cachedir, self.filename)
                self.filename = nextFreeFilename (self.filename)
                needsSave = True
            try:
                os.remove (self.filename)
            except:
                pass
            try:
                os.rename (tmp_filename, self.filename)
            except:
                self.filename = None
                needsSave = True
        
            if (info.has_key ("etag")):
                etag = unicodify(info["etag"])
            else:
                etag = None

            if (info.has_key ("modified")):
                modified = unicodify(info["modified"])
            else:
                modified = None

            if self.etag != etag:
                needsSave = True
                self.etag = etag
            if self.modified != modified:
                needsSave = True
                self.modified = modified
            if self.url != url:
                needsSave = True
                self.url = url
            self.updated = True
        finally:
            if needsChange:
                self.dbItem.signalChange(needsSave=needsSave)
            self.updating = False
            if self.needsUpdate:
                self.needsUpdate = False
                self.requestUpdate()
            iconCacheUpdater.updateFinished ()

    def requestIcon (self):
        if self.removed:
            iconCacheUpdater.updateFinished()
            return

        self.dbItem.confirmDBThread()
        if (self.updating):
            self.needsUpdate = True
            iconCacheUpdater.updateFinished ()
            return
        try:
            url = self.dbItem.getThumbnailURL()
        except:
            url = self.url

        # Only verify each icon once per run unless the url changes
        if (self.updated and url == self.url and url is not None):
            iconCacheUpdater.updateFinished ()
            return

        self.updating = True

        # No need to extract the icon again if we already have it.
        if url is not None and (url.startswith(u"/") or url.startswith(u"file://")):
            iconCacheUpdater.updateFinished ()
            return

        # But if we don't have it, let's extract it from the movie file if we 
        # can get a valid filename and if the item is currently not being 
        # downloaded (otherwise we could get some pretty bad random crashes).
        hasFileName = (hasattr(self.dbItem, u'getFilename') and self.dbItem.getFilename() != u'')
        isDownloading = (hasattr(self.dbItem, u'getState') and self.dbItem.getState() == u'downloading')
        if url is None and hasFileName and not isDownloading:
            self.extractIconFromMovieFile()
            return
        
        # Still nothing and no url? Bail.
        if url is None:
            self.errorCallback(url)
            return

        # Last try, get the icon from HTTP.
        if (url == self.url and self.filename and os.access (self.filename, os.R_OK)):
            httpclient.grabURL (url, lambda info: self.updateIconCache(url, info), lambda error: self.errorCallback(url, error), etag=self.etag, modified=self.modified)
        else:
            httpclient.grabURL (url, lambda info: self.updateIconCache(url, info), lambda error: self.errorCallback(url, error))

    @asIdle
    def extractIconFromMovieFile(self):
        iconData = None
        filename = self.dbItem.getFilename()

        # Try all extractors. The first one to succeed wins.
        for extract in iconExtractors:
            iconData = extract(filename, 0.5)
            if iconData is not None:
                break

        if iconData is None:
            self.errorCallback(None)
            return
        
        iconFilename, unused = os.path.splitext(os.path.basename(filename))
        iconFilename = '%s.jpg' % iconFilename
        
        # The updateIconCache method expects HTTP-like results.
        # So let's just pretend we  got the icon from an HTTP request :)
        info = dict()
        info['status'] = 200
        info['body'] = iconData
        info['filename'] = iconFilename
        
        self.updateIconCache(None, info)
        self.url = u'file://%s' % makeURLSafe(self.filename)

    def requestUpdate (self, is_vital = False):
        if hasattr (self, "updating") and hasattr (self, "dbItem"):
            if self.removed:
                return

            iconCacheUpdater.requestUpdate (self, is_vital = is_vital)

    def onRestore(self):
        self.removed = False
        self.updated = False
        self.updating = False
        self.needsUpdate = False
        self.requestUpdate ()

    def isValid(self):
        self.dbItem.confirmDBThread()
        return self.filename is not None and os.path.exists(self.filename)

    def getFilename(self):
        self.dbItem.confirmDBThread()
        if self.url and self.url.startswith (u"file://"):
            return self.url[len(u"file://"):]
        elif self.url and self.url.startswith (u"/"):
            return self.url
        else:
            return self.filename
