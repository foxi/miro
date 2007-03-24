const VLCRENDERER_CONTRACTID = "@participatoryculture.org/dtv/vlc-renderer;1";
const VLCRENDERER_CLASSID = Components.ID("{F9F01D99-9D3B-4A69-BD5F-285FFD360079}");

var pybridge = Components.classes["@participatoryculture.org/dtv/pybridge;1"].
        getService(Components.interfaces.pcfIDTVPyBridge);
var jsbridge = Components.classes["@participatoryculture.org/dtv/jsbridge;1"].
        getService(Components.interfaces.pcfIDTVJSBridge);

function writelog(str) {
    Components.classes['@mozilla.org/consoleservice;1']
	.getService(Components.interfaces.nsIConsoleService)	
	.logStringMessage(str);
}

function VLCRenderer() { 
  this.scheduleUpdates = false;
}

VLCRenderer.prototype = {
  QueryInterface: function(iid) {
    if (iid.equals(Components.interfaces.pcfIDTVVLCRenderer) ||
      iid.equals(Components.interfaces.nsISupports))
      return this;
    throw Components.results.NS_ERROR_NO_INTERFACE;
  },

  init: function(window) {
    this.document = window.document;
    var videoBrowser = this.document.getElementById("mainDisplayVideo");
    this.vlc = videoBrowser.contentDocument.getElementById("video1");
    this.timer = Components.classes["@mozilla.org/timer;1"].
          createInstance(Components.interfaces.nsITimer);
    this.timer2 = Components.classes["@mozilla.org/timer;1"].
          createInstance(Components.interfaces.nsITimer);
    this.active = false;
    this.startedPlaying = false;
    this.item = null;
    this.playTime = 0;
  },

  doScheduleUpdates: function() {
      var callback = {
	  notify: function(timer) { this.parent.updateVideoControls()}
      };
      callback.parent = this;
      this.timer.initWithCallback(callback, 500,
				  Components.interfaces.nsITimer.TYPE_ONE_SHOT);
  },

  updateVideoControls: function() {
    try {
      var elapsed = 0;
      var len = 1;
      if (this.active) {
  	if(this.vlc.playlist.isPlaying) {
  	    this.startedPlaying = true;
  	    elapsed = this.vlc.input.time;
  	    len = this.vlc.input.length;
  	    if (len < 1) len = 1;
  	    if (elapsed < 0) elapsed = 0;
  	    if (elapsed > len) elapsed = len;
  	} else if (this.startedPlaying) {
  	    // hit the end of the playlist
            this.active = false;
  	    this.scheduleUpdates = false;
  	    pybridge.onMovieFinished();
  	}
  
  	var progressSlider = this.document.getElementById("progress-slider");
  	if(!progressSlider.beingDragged) {
  	    jsbridge.setSliderText(elapsed);
  	    jsbridge.moveSlider(elapsed/len);
  	}
      }
      if(this.scheduleUpdates) {
	  this.doScheduleUpdates();
      }
    } catch (e) {
      if (this.startedPlaying) {
	// probably hit the end of the playlist in the middle of this function
        this.scheduleUpdates = false;
        this.active = false;
	pybridge.onMovieFinished();
      } else if(this.scheduleUpdates) {
	  this.doScheduleUpdates();
      }
    }
  },

  resetVideoControls: function () {
     jsbridge.setSliderText(0);
     jsbridge.moveSlider(0);
  },

  showPauseButton: function() {
    var playButton = this.document.getElementById("bottom-buttons-play");
    playButton.className = "bottom-buttons-pause";
    var playMenuItem = this.document.getElementById('menuitem-play');
    playMenuItem.label = playMenuItem.getAttribute("pause-label");
  },

  showPlayButton: function() {
    var playButton = this.document.getElementById("bottom-buttons-play");
    playButton.className = "bottom-buttons-play";
    var playMenuItem = this.document.getElementById('menuitem-play');
    playMenuItem.label = playMenuItem.getAttribute("play-label");
  },

  reset: function() {
    // We don't need these, and stops seem to cause problems, so I'm
    // commenting them out --NN
    // this.stop();
    // this.vlc.clear_playlist();
    this.showPlayButton();
    this.resetVideoControls();
  },

  canPlayURL: function(url) {
    return true;
  },

  selectURL: function(url) {
    // FIXME: This doesn't quite follow the interface since we shouldn't be
    // playing the item at this point.  However currently, all calls to
    // selectItem are followed immediately by play, so this doesn't matter.
    // Also, VLC seems to have problems with quickly stopping and playing.

    // It appears that clear_playlist() always leaves one item in
    // the playlist. This is the only way I could figure out to
    // actually clear it... -NN  
      var item;
      if (this.vlc.playlist.items.count > 0) {
          this.stop();
          this.vlc.playlist.items.clear();
      }
      this.item = this.vlc.playlist.add(url);
  },

  setCurrentTime: function(time) {
      try {
	  this.vlc.input.time = time * 1000;
      } catch (e) {
	  var callback = {
	      notify: function(timer) {
		  this.parent.setCurrentTime(this.parent.playTime);
	      }
	  };
	  callback.parent = this;
	  this.playTime = time;
	  this.timer2.initWithCallback(callback, 10,
				       Components.interfaces.nsITimer.TYPE_ONE_SHOT);
      }
    },
  
  play: function() {
      if (this.vlc.playlist.items.count > 0) {
	  if(!this.vlc.playlist.isPlaying) {
	      if (this.item != null) {
		  this.vlc.playlist.playItem(this.item);
		  this.item = null;
	      } else {
		  this.vlc.playlist.play();
	      }
	  } 
	  this.scheduleUpdates = true;
	  this.active = true;
	  this.startedPlaying = false;
	  this.doScheduleUpdates();
	  this.showPauseButton();
      } else {
	  this.active = false;
	  this.scheduleUpdates = false;
	  pybridge.onMovieFinished();
      }
  },

  playFromTime: function(time) {
      this.play();
      this.setCurrentTime(time);
  },

  pause: function() {
    this.scheduleUpdates = false;
    this.active = false;
    if (this.vlc.playlist.isPlaying) {
        if (this.vlc.playlist.items.count > 0) {
            this.vlc.playlist.togglePause();
        }
    }
    this.showPlayButton();
  },

  pauseForDrag: function() {
    this.scheduleUpdates = false;
    this.active = false;
    if (this.vlc.playlist.isPlaying) {
        if (this.vlc.playlist.items.count > 0) {
            this.vlc.playlist.togglePause();
        }
    }
  },

  stop: function() {
    this.scheduleUpdates = false;
    this.active = false;
    if (this.vlc.playlist.items.count > 0) {
        this.vlc.playlist.stop();
    }
    this.showPlayButton();
    this.resetVideoControls();
  },

  goToBeginningOfMovie: function() {
    this.vlc.input.time = 0;
  },

  getDuration: function() {
    rv = this.vlc.input.length;
    return rv;
  },

  getCurrentTime: function() {
      var rv;
      rv = this.vlc.input.time;
      return rv / 1000.0;
  },

  setVolume: function(level) {
    this.vlc.audio.volume = level*200;
  },

  goFullscreen: function() {
    this.vlc.video.fullscreen = true;
  },
};

var Module = {
  _classes: {
      VLCRenderer: {
          classID: VLCRENDERER_CLASSID,
          contractID: VLCRENDERER_CONTRACTID,
          className: "DTV VLC Renderer",
          factory: {
              createInstance: function(delegate, iid) {
                  if (delegate)
                      throw Components.results.NS_ERROR_NO_AGGREGATION;
                  return new VLCRenderer().QueryInterface(iid);
              }
          }
      }
  },

  registerSelf: function(compMgr, fileSpec, location, type) {
      var reg = compMgr.QueryInterface(
          Components.interfaces.nsIComponentRegistrar);

      for (var key in this._classes) {
          var c = this._classes[key];
          reg.registerFactoryLocation(c.classID, c.className, c.contractID,
                                      fileSpec, location, type);
      }
  },

  getClassObject: function(compMgr, cid, iid) {
      if (!iid.equals(Components.interfaces.nsIFactory))
          throw Components.results.NS_ERROR_NO_INTERFACE;

      for (var key in this._classes) {
          var c = this._classes[key];
          if (cid.equals(c.classID))
              return c.factory;
      }

      throw Components.results.NS_ERROR_NOT_IMPLEMENTED;
  },

  canUnload: function (aComponentManager) {
      return true;
  }
};

function NSGetModule(compMgr, fileSpec) {
  return Module;
}
