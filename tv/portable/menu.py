from gettext import gettext as _

import app
import eventloop
import tabs

def makeMenu(items):
    """Convenience function to create a list of MenuItems given on a list of
    (callback, label) tuples.
    """

    return [MenuItem(callback, label) for callback, label in items]

class MenuItem:
    """A single menu item in a context menu.

    Normally frontends should display label as the text for this menu item,
    and if it's clicked on call activate().  One second case is if label is
    blank, in which case a separator should be show.  Another special case is
    if callback is None, in which case the label should be shown, but it
    shouldn't be clickable.  
    
    """

    def __init__(self, callback, label):
        self.label = label
        self.callback = callback

    def activate(self):
        """Run this menu item's callback in the backend event loop."""

        eventloop.addUrgentCall(self.callback, "context menu callback")

def makeContextMenu(templateName, view, selection, clickedID):
    if len(selection.currentSelection) == 1:
        obj = selection.getObjects()[0]
        if isinstance(obj, tabs.Tab):
            obj = obj.obj
        return obj.makeContextMenu(templateName, view)
    else:
        type = selection.getType()
        objects = selection.getObjects()
        if type == 'item':
            return makeMultiItemContextMenu(templateName, view, objects,
                    clickedID)
        elif type == "playlisttab":
            return makeMenu([
                (app.controller.removeCurrentPlaylist, _('Remove')),
            ])
        elif type == "channeltab":
            return makeMenu([
                (app.controller.updateCurrentFeed, _('Update Channels Now')),
                (app.controller.removeCurrentFeed, _('Remove')),
            ])
        else:
            return None

def makeMultiItemContextMenu(templateName, view, selectedItems, clickedID):
    c = app.controller # easier/shorter to type
    watched = downloaded = downloading = available = 0
    for i in selectedItems:
        if i.getState() == 'downloading':
            downloading += 1
        elif i.isDownloaded():
            downloaded += 1
            if i.getSeen():
                watched += 1
        else:
            available += 1

    items = []
    if downloaded > 0:
        items.append((None, _('%d Downloaded Items') % downloaded))
        items.append((lambda: c.playView(view, clickedID),
            _('Play')))
        items.append((c.addToNewPlaylist, _('Add to new playlist')))
        if templateName in ('playlist', 'playlist-folder'):
            label = _('Remove From Playlist')
        else:
            label = _('Remove From My Collection')
        items.append((c.removeCurrentItems, label))
        if watched:
            def markAllUnseen():
                for item in selectedItems:
                    item.markItemUnseen()
            items.append((markAllUnseen, _('Mark as Unwatched')))

    if available > 0:
        if len(items) > 0:
            items.append((None, ''))
        items.append((None, _('%d Available Items') % available))
        items.append((app.controller.downloadCurrentItems, _('Download')))

    if downloading:
        if len(items) > 0:
            items.append((None, ''))
        items.append((None, _('%d Downloading Items') % downloading))
        items.append((app.controller.stopDownloadingCurrentItems, 
            _('Cancel Download')))
        items.append((app.controller.pauseDownloadingCurrentItems, 
            _('Pause Download')))

    return makeMenu(items)
