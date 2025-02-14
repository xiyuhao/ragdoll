import os
import re
import sys
import time
import json
import ctypes
import logging
import datetime

from maya import cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from maya.OpenMayaUI import MQtUtil
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from PySide2 import QtCore, QtWidgets, QtGui
import shiboken2

from . import options, licence, dump, internal as i__, __
from .vendor import qargparse, markdown, qjsonmodel

try:
    from maya.cmds import optionVar
except ImportError:
    # Standalone
    def optionVar(*args, **kwargs):
        return None

log = logging.getLogger("ragdoll")

self = sys.modules[__name__]
self._maya_window = None
self._dpi = None

# Expose some utility to interactive.py
isValid = shiboken2.isValid


def camel_to_title(text):
    """Convert camelCase `text` to Title Case

    Example:
        >>> camel_to_title("mixedCase")
        "Mixed Case"
        >>> camel_to_title("myName")
        "My Name"
        >>> camel_to_title("you")
        "You"
        >>> camel_to_title("You")
        "You"
        >>> camel_to_title("This is That")
        "This Is That"

    """

    return re.sub(
        r"((?<=[a-z])[A-Z]|(?<!\A)[A-Z](?=[a-z]))",
        r" \1", text
    ).title()


def _resource(*fname):
    dirname = os.path.dirname(__file__)
    resdir = os.path.join(dirname, "resources")
    return os.path.normpath(os.path.join(resdir, *fname))


with open(_resource("ui", "style.css")) as f:
    stylesheet = f.read()


def _scaled_stylesheet(style):
    """Replace any mention of <num>px with scaled version

    This way, you can still use px without worrying about what
    it will look like at HDPI resolution.

    """

    output = []
    for line in style.splitlines():
        line = line.rstrip()
        if line.endswith("px;"):
            key, value = line.rsplit(" ", 1)
            value = px(int(value[:-3]))
            line = "%s %dpx;" % (key, value)
        output += [line]
    return "\n".join(output)


def _with_entered_exited_signals(cls):
    class WidgetHoverFactory(cls):
        entered = QtCore.Signal()
        exited = QtCore.Signal()

        def enterEvent(self, event):
            self.entered.emit()
            return super(WidgetHoverFactory, self).enterEvent(event)

        def leaveEvent(self, event):
            self.exited.emit()
            return super(WidgetHoverFactory, self).leaveEvent(event)

    return WidgetHoverFactory


def hide_menuitem(item):
    ptr = MQtUtil.findMenuItem(item)
    item = shiboken2.wrapInstance(i__.long(ptr), QtWidgets.QAction)
    item.setVisible(False)


def show_menuitem(item):
    ptr = MQtUtil.findMenuItem(item)
    item = shiboken2.wrapInstance(i__.long(ptr), QtWidgets.QAction)
    item.setVisible(True)


def MayaWindow():
    """Fetch Maya window, along with whatever DPI it's got cooking"""

    # This will never change during the lifetime of this Python session
    if not self._maya_window:
        ptr = MQtUtil.mainWindow()

        if ptr is not None:
            self._maya_window = shiboken2.wrapInstance(
                i__.long(ptr), QtWidgets.QMainWindow
            )

        else:

            # Running standalone
            return None

    return self._maya_window


def px(value):
    """Return a scaled value, for HDPI resolutions"""

    if not self._dpi:
        # We can get system DPI from a window handle,
        # but I haven't figured out how to get a window handle
        # without first making a widget. So here we make one
        # as invisibly as we can, as an invisible tooltip.
        # This doesn't create a taskbar icon nor changes focus
        # and in fact *should* happen without any noticeable effect
        # to the user. Welcome to provide a less naughty alternative
        any_widget = QtWidgets.QWidget()
        any_widget.setWindowFlags(QtCore.Qt.ToolTip)
        any_widget.show()
        window = any_widget.windowHandle()

        # E.g. 1.5 or 2.0
        scale = window.screen().logicalDotsPerInch() / 96.0
        log.debug("DPI scale was %.1fx" % scale)

        # Store for later
        self._dpi = scale

    return value * self._dpi


class Player(QtWidgets.QWidget):
    frame_changed = QtCore.Signal(int)

    def __init__(self, media, parent=None):
        super(Player, self).__init__(parent=parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        panels = {
            "overlay": QtWidgets.QWidget(self),
            "video": QtWidgets.QStackedWidget()
        }

        widgets = {
            "background": QtWidgets.QWidget(),
            "title": QtWidgets.QLabel("Title"),
            "description": QtWidgets.QLabel("Description here")
        }

        effects = {
            "background": (
                QtWidgets.QGraphicsOpacityEffect(widgets["background"])
            ),
            "title": (
                QtWidgets.QGraphicsOpacityEffect(widgets["title"])
            ),
            "description": (
                QtWidgets.QGraphicsOpacityEffect(widgets["description"])
            ),
        }

        animation = {
            "overlay": (
                QtCore.QPropertyAnimation(self, b"overlay_height")
            ),
            "background": (
                QtCore.QPropertyAnimation(effects["background"], b"opacity")
            ),
            "title": (
                QtCore.QPropertyAnimation(effects["title"], b"opacity")
            ),
            "description": (
                QtCore.QPropertyAnimation(effects["description"], b"opacity")
            ),
        }

        for key in ("background", "title", "description"):
            effect = effects[key]
            widgets[key].setGraphicsEffect(effect)
            effect.setOpacity(0.0)

        for name, obj in widgets.items():
            obj.setObjectName(name)

        for name, obj in panels.items():
            obj.setObjectName(name)

        widgets["description"].setWordWrap(True)

        layout = QtWidgets.QVBoxLayout(panels["overlay"])
        layout.addWidget(widgets["title"], QtCore.Qt.AlignVCenter)
        layout.addWidget(widgets["description"], QtCore.Qt.AlignVCenter)
        layout.addWidget(QtWidgets.QWidget(), 1)  # Push up

        widgets["background"].setParent(panels["overlay"])
        widgets["background"].setAttribute(QtCore.Qt.WA_StyledBackground)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panels["video"])

        for anim in animation.values():
            anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)

        animation["title"].setDuration(100)
        animation["background"].setDuration(100)
        animation["description"].setDuration(100)
        animation["overlay"].setDuration(50)

        self._panels = panels
        self._widgets = widgets
        self._effects = effects
        self._animation = animation
        self._clips = []

        self._offset = 0
        self._frame_to_index = []

        t0 = time.time()

        for clip in media:
            self.add_clip(clip)

        t1 = time.time()
        ms = (t1 - t0) * 1000
        log.debug("Created player in %.2f ms" % ms)

    def add_clip(self, clip):
        fname = _resource("gif", clip["fname"])
        movie = QtGui.QMovie(parent=self)
        movie.setFileName(fname)

        # Cache all frames in advance, for blissful scrubbing
        if options.read("cacheMedia") == "All":
            # Unfortunately, every frame takes ~1.5 mb of memory
            # and 1,000 frames is not uncommon!
            movie.setCacheMode(movie.CacheAll)
            movie.jumpToFrame(movie.frameCount() - 1)

        # Fit the 480p video to a 570px wide UI
        movie.setScaledSize(QtCore.QSize(px(570), px(320)))

        widget = QtWidgets.QLabel()
        widget.setMovie(movie)

        video = self._panels["video"]
        video.addWidget(widget)

        # Maintain reference to stack index for each frame
        new_index = video.count() - 1
        offset = len(self._frame_to_index)
        for frame in range(movie.frameCount()):
            self._frame_to_index.append((new_index, offset))

        movie.frameChanged.connect(self.on_frame_changed)

        self._clips.append(clip)

    def on_frame_changed(self, frame):
        self.frame_changed.emit(frame + self._offset)

    def index_at(self, frame):
        """Find index at which player is at from a global time"""
        return self._frame_to_index[frame]

    def set_current_index(self, index):
        # Update help card
        clip = self._clips[index]

        self._widgets["title"].setText(clip["label"])
        self._widgets["description"].setText(clip["description"])

        # Update offset
        offset = 0

        for i in range(index):
            video = self.video(i)
            offset += video.movie().frameCount()

        self._offset = offset

        video = self._panels["video"]
        return video.setCurrentIndex(index)

    def current_index(self):
        return self._panels["video"].currentIndex()

    def current_video(self):
        return self._panels["video"].currentWidget()

    def video(self, index):
        return self._panels["video"].widget(index)

    def jump_to(self, frame=0):
        index, offset = self.index_at(frame)

        # From global to local frame number
        frame = frame - offset

        if index != self.current_index():
            self.set_current_index(index)

        player = self.current_video()
        movie = player.movie()

        # If media is cached, then jumping to
        # any frame from any frame is possible.
        if options.read("cacheMedia") == "All":
            movie.jumpToFrame(frame)

        elif options.read("cacheMedia") == "On":

            # Caching is much preferred, but if memory
            # is an issue then we haven't got much choice.
            current = movie.currentFrameNumber()

            # I.e. the user scrubs backwards
            # The QMovie apparently doesn't scrub that way
            if frame < current:
                current = 0

            # A QMovie that isn't cached must be jumped
            # one frame at a time, in the order they appear
            for f in range(current, frame):
                movie.jumpToFrame(f)

        else:
            return movie.jumpToFrame(0)

    def play(self):
        video = self.current_video()

        if not video:
            return

        video.movie().setPaused(False)

    def pause(self):
        video = self.current_video()

        if not video:
            return

        video.movie().setPaused(True)

    def start(self, index=0):
        self.set_current_index(index)
        video = self.current_video()

        if not video:
            return

        video.movie().start()

    def stop(self):
        video = self.current_video()

        if not video:
            return

        video.movie().stop()

    def enterEvent(self, event):
        """Animate overlay transition on entering and leaving"""
        anim = self._animation["background"]
        anim.setStartValue(0.0)
        anim.setEndValue(0.9)
        anim.start()

        # anim = self._animation["overlay"]
        # anim.setStartValue(px(50))
        # anim.setEndValue(px(80))
        # anim.start()

        for text in ("title", "description"):
            anim = self._animation[text]
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.start()

        super(Player, self).enterEvent(event)

    def leaveEvent(self, event):
        anim = self._animation["background"]
        anim.setStartValue(0.9)
        anim.setEndValue(0.0)
        anim.start()

        # anim = self._animation["overlay"]
        # anim.setStartValue(px(80))
        # anim.setEndValue(px(50))
        # anim.start()

        for text in ("title", "description"):
            anim = self._animation[text]
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.start()

        super(Player, self).leaveEvent(event)

    def _get_overlay_height(self):
        if not hasattr(self, "_panels"):
            return 0.0

        return self._panels["overlay"].height()

    def _set_overlay_height(self, height):
        if not hasattr(self, "_panels"):
            return

        overlay = self._panels["overlay"]
        overlay.setFixedHeight(height)
        overlay.move(0, self.size().height() - height)

    overlay_height = QtCore.Property(
        float, _get_overlay_height, _set_overlay_height)

    def resizeEvent(self, event):
        """Fit widgets without layout"""
        self._widgets["background"].resize(event.size())

        overlay = self._panels["overlay"]
        overlay_size = QtCore.QSize(event.size().width(), px(80))
        overlay.resize(overlay_size)

        height = px(80)
        overlay.setFixedHeight(height)
        overlay.move(0, self.size().height() - height)

        super(Player, self).resizeEvent(event)

    def showEvent(self, event):
        super(Player, self).showEvent(event)

        self._widgets["background"].lower()
        self._widgets["background"].show()
        self._panels["overlay"].raise_()
        self._panels["overlay"].show()



class DirectJumpSlider(QtWidgets.QSlider):
    """Slider with support for clicking anywhere to scrubbing

    From: https://stackoverflow.com/questions/11132597
                /qslider-mouse-direct-jump

    """

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            if self.orientation() == QtCore.Qt.Horizontal:
                self.setValue(
                    self.minimum() + (self.maximum() - self.minimum() + 1) *
                    float(event.x()) / float(self.width())
                )
            else:
                self.setValue(
                    self.minimum() + (
                        (self.maximum() - self.minimum()) * event.x()
                    ) / self.width()
                )

            event.accept()

        return super(DirectJumpSlider, self).mousePressEvent(event)


class Clip(QtWidgets.QWidget):
    def __init__(self, fname, parent=None):
        super(Clip, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        movie = QtGui.QMovie(fname, parent=self)
        movie.jumpToFrame(0)
        image = movie.currentImage()

        # Fit more pixels in each thumbnail
        image = image.scaledToHeight(
            px(image.height()) * 0.25,
            QtCore.Qt.SmoothTransformation
        )

        self._label = QtWidgets.QLabel(self)
        self._length = movie.frameCount()
        self._fname = fname
        self._movie = movie
        self._image = image
        self._scale = 1.0
        self._height = px(75)

        self._label.setAttribute(QtCore.Qt.WA_StyledBackground)

        self.fit()

        anim = QtCore.QPropertyAnimation(self, b"animated_height")
        anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        anim.setDuration(200)

        self._height_anim = anim

        self.animate_height()

    def showEvent(self, event):
        self._label.show()
        super(Clip, self).showEvent(event)

    def animate_height(self, height=None):
        height = height or self._height
        current = self._height_anim.currentValue()
        self._height_anim.setStartValue(current)
        self._height_anim.setEndValue(height)
        self._height_anim.start()

    def _get_animated_height(self):
        return self._height

    def _set_animated_height(self, height):
        self._height = height
        self._label.setFixedHeight(height)

        # Centered motion
        center = self.size().height() / 2.0
        height_delta = height / 2.0
        center_delta = height - center
        y = height_delta - center_delta
        self._label.move(0, y)

    # PySide's properties have garbage syntax
    animated_height = QtCore.Property(
        float, _get_animated_height, _set_animated_height)

    def fit(self, scale=1.0):
        """Fit thumbnail inside of clip

        Scale is whatever the parent container needs it
        to be in order to fill a complete with of many clips.

        """

        image = self._image

        original_width = image.width()
        scaled_width = self._length * scale

        # Ensure we've got enough pixels to fill the width
        if original_width < scaled_width:
            scale = scaled_width / float(original_width)
            image = image.scaledToWidth(
                original_width * scale, QtCore.Qt.SmoothTransformation
            )

        rect = image.rect()
        rect.setHeight(px(80))  # Crop from bottom
        rect.setWidth(scaled_width)  # Crop from right
        image = image.copy(rect)

        self._label.setPixmap(QtGui.QPixmap.fromImage(image))
        self._label.setFixedWidth(scaled_width)
        self.setFixedWidth(scaled_width)

    @property
    def length(self):
        return self._length

    @property
    def movie(self):
        return self._movie

    @property
    def image(self):
        return self._image


class Timeline(QtWidgets.QWidget):
    pressed = QtCore.Signal()
    moved = QtCore.Signal(int)
    released = QtCore.Signal()

    def __init__(self, media, parent=None):
        super(Timeline, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)
        self.setCursor(QtCore.Qt.SizeHorCursor)

        background = QtWidgets.QWidget(self)
        background.setObjectName("background")
        background.setAttribute(QtCore.Qt.WA_StyledBackground)

        layout = QtWidgets.QHBoxLayout(background)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        slider = DirectJumpSlider(self)
        slider.setOrientation(QtCore.Qt.Horizontal)

        slider.sliderMoved.connect(self.on_slider_moved)
        slider.sliderPressed.connect(self.on_slider_pressed)
        slider.sliderReleased.connect(self.on_slider_released)
        slider.valueChanged.connect(self.on_slider_changed)

        self._slider = slider
        self._background = background
        self._clips = []
        self._frame_to_index = []
        self._current_index = 0

        for clip in media or []:
            self.add_clip(clip)

        if media:
            self.on_index_changed(0)

    def index_at(self, frame):
        """Find index at which player is at from a global time"""
        return self._frame_to_index[frame]

    def jump_to(self, frame):
        self.blockSignals(True)
        self._slider.setValue(frame)
        self.blockSignals(False)

    def on_slider_changed(self, frame):
        index = self.index_at(frame)
        if index != self._current_index:
            self.on_index_changed(index)
            self._current_index = index

    def on_index_changed(self, index):
        for clip in self._clips:
            clip.animate_height(px(55))
            clip.setProperty("playing", False)
            clip.setStyleSheet("")  # Respond to edited attributes

        clip = self._clips[index]
        clip.animate_height(px(60))
        clip.setProperty("playing", True)
        clip.setStyleSheet("")

    def on_slider_pressed(self,):
        frame = self._slider.value()
        self.pressed.emit()
        self.moved.emit(frame)

    def on_slider_moved(self, frame):
        self.moved.emit(frame)

    def on_slider_released(self):
        self.released.emit()

    def add_clip(self, clip):
        fname = _resource("gif", clip["fname"])

        assert os.path.exists(fname), "'%s' could not be found" % fname
        assert fname.endswith(".gif"), "'%s' was not a gif" % fname

        clip = Clip(fname)
        clip.setProperty("scaledLength", -1)
        clip.setProperty("scale", 1.0)

        layout = self._background.layout()
        layout.addWidget(clip)

        self._clips.append(clip)

        total_count = 0
        for clip in self._clips:
            total_count += clip.length

        self._slider.setRange(0, total_count - 1)

        # Maintain reference to stack index for each frame
        for frame in range(clip.length):
            self._frame_to_index.append(len(self._clips) - 1)

    def showEvent(self, event):
        """Give it a little push

        Without this, the timeline can appear at 0 height
        when used in a layout. There's probably a more correct
        solution to this, if you spot it do something about it.

        """

        self.on_index_changed(0)

        return super(Timeline, self).showEvent(event)

    def resizeEvent(self, event):
        """Keep clips proportionally sized to the overall timeline"""
        self._slider.resize(event.size())
        self._background.resize(event.size())

        scale = event.size().width() / float(self._slider.maximum())

        for clip in self._clips:
            clip.fit(scale)

        return super(Timeline, self).resizeEvent(event)


class HelpPage(QtWidgets.QWidget):
    """The widget showing up when the user clicked Help

     _____________________________________
    |      _                              |
    | <-- |_| Active Rigid       Options  |
    |         ----------------   o ----   |
    |                            o ----   |
    |         ----------------   o ----   |
    |         ---------------             |
    |         -------------      Media    |
    |         --------------     o ----   |
    |         ---------------    o ----   |
    |                            o ----   |
    |                            o ----   |
    |                                     |
    |_____________________________________|

    """

    back = QtCore.Signal()

    def __init__(self, parent=None):
        super(HelpPage, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)

        widgets = {
            "background": QtWidgets.QLabel(),
            "body": QtWidgets.QWidget(),
            "scroll": QtWidgets.QScrollArea(),

            "back": QtWidgets.QPushButton(),
            "title": QtWidgets.QLabel("Label"),
            "icon": QtWidgets.QLabel(),
            "summary": QtWidgets.QLabel(),
            "description": QtWidgets.QLabel(),

            "sidebar": QtWidgets.QWidget(),
            "mediaTitle": QtWidgets.QLabel("Media"),
            "media": QtWidgets.QWidget(),
            "optionsTitle": QtWidgets.QLabel("Options"),
            "options": QtWidgets.QWidget(),

            "logo": QtWidgets.QLabel(),
        }

        widgets["background"].setPixmap(_resource("ui", "background1.png"))
        widgets["background"].setScaledContents(True)
        widgets["background"].setParent(self)

        widgets["title"].setAlignment(QtCore.Qt.AlignLeft)

        opacity = QtWidgets.QGraphicsOpacityEffect(widgets["background"])
        opacity.setOpacity(0.5)
        widgets["background"].setGraphicsEffect(opacity)

        icon = _resource("icons", "back.png")
        icon = QtGui.QIcon(icon)
        widgets["back"].setIcon(icon)
        widgets["back"].clicked.connect(self.on_back)

        widgets["icon"].setFixedWidth(px(20))
        widgets["icon"].setAlignment(QtCore.Qt.AlignHCenter)

        font = widgets["summary"].font()
        font.setWeight(font.Bold)

        layout = QtWidgets.QVBoxLayout(widgets["media"])
        layout.addWidget(widgets["mediaTitle"])
        layout.addWidget(QtWidgets.QWidget(), 1)  # Push everything up
        layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(widgets["options"])
        layout.addWidget(widgets["optionsTitle"])
        layout.addWidget(QtWidgets.QWidget(), 1)  # Push everything up
        layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(widgets["sidebar"])
        layout.addWidget(widgets["media"])
        layout.addWidget(widgets["options"])
        layout.addWidget(QtWidgets.QWidget(), 1)  # Push everything up
        layout.setSpacing(px(20))

        pixmap = _resource("ui", "ragdoll_silhouette_white_128.png")
        pixmap = QtGui.QPixmap(pixmap)
        pixmap = pixmap.scaledToWidth(
            px(32), QtCore.Qt.SmoothTransformation
        )
        widgets["logo"].setPixmap(pixmap)

        for label in (widgets["summary"], widgets["description"]):
            label.setWordWrap(True)
            label.setAlignment(QtCore.Qt.AlignTop)
            label.setTextInteractionFlags(
                QtCore.Qt.TextBrowserInteraction
            )

        for name, obj in widgets.items():
            obj.setObjectName(name)

        layout = QtWidgets.QGridLayout(widgets["body"])
        layout.addWidget(widgets["back"], 0, 0, QtCore.Qt.AlignCenter)
        layout.addWidget(widgets["icon"], 0, 1, QtCore.Qt.AlignCenter)
        layout.addWidget(widgets["title"], 0, 2)
        layout.addWidget(widgets["summary"], 1, 1, 1, 2)
        layout.addWidget(widgets["description"], 2, 1, 1, 2)
        layout.addWidget(widgets["sidebar"], 0, 3, 3, 1)
        layout.addWidget(widgets["logo"], 10, 3, QtCore.Qt.AlignRight)
        layout.setRowStretch(2, 1)  # Push summary upwards
        layout.setColumnStretch(2, 1)  # Push icon leftwards
        layout.setVerticalSpacing(px(5))
        layout.setHorizontalSpacing(px(10))
        layout.setContentsMargins(px(10), px(10), px(10), px(10))

        widgets["scroll"].setWidgetResizable(True)
        widgets["scroll"].setFrameShape(QtWidgets.QFrame.NoFrame)
        widgets["scroll"].setWidget(widgets["body"])

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(widgets["scroll"])
        layout.setContentsMargins(0, 0, 0, 0)

        self._widgets = widgets
        self.setStyleSheet(_scaled_stylesheet(stylesheet))

    def showEvent(self, event):
        self._widgets["background"].show()
        return super(HelpPage, self).showEvent(event)

    def resizeEvent(self, event):
        self._widgets["background"].resize(event.size())
        return super(HelpPage, self).resizeEvent(event)

    def on_back(self):
        self.back.emit()

    def load(self, key):
        item = __.menuitems[key]

        # Use a fallback in case there isn't an icon
        label = item.get("label", camel_to_title(key))
        icon = item.get("icon", "system.png")
        summary = item.get("summary", "")
        description = item.get("description", "")
        options = item.get("options", [])
        media = item.get("media", [])

        # Pre-process any contained markdown
        summary = markdown.markdown(summary)
        description = markdown.markdown(description)

        pixmap = QtGui.QPixmap(_resource("icons", icon))
        pixmap = pixmap.scaledToWidth(
            px(20), QtCore.Qt.SmoothTransformation
        )

        self._widgets["title"].setText(label)
        self._widgets["icon"].setPixmap(pixmap)
        self._widgets["summary"].setText(summary)
        self._widgets["description"].setText(description)

        def clear(layout):
            while layout.count() > 1:
                item = layout.takeAt(1)
                widget = item.widget()

                if widget:
                    widget.deleteLater()
                del item

        options_layout = self._widgets["options"].layout()
        media_layout = self._widgets["media"].layout()

        self._widgets["optionsTitle"].setText("Options (%d)" % len(options))
        self._widgets["mediaTitle"].setText("Media (%d)" % len(media))

        clear(options_layout)
        clear(media_layout)

        if options:
            for option in options:
                option = QtWidgets.QLabel("- " + option)
                options_layout.addWidget(option)
        else:
            options_layout.addWidget(QtWidgets.QLabel("- No options"))

        if media:
            for med in media:
                med = QtWidgets.QLabel("- " + med["label"])
                media_layout.addWidget(med)
        else:
            media_layout.addWidget(QtWidgets.QLabel("- No media"))


class Options(QtWidgets.QMainWindow):
    def __init__(self,
                 key,
                 args,
                 command,
                 icon,
                 description,
                 media=None,
                 parent=None):

        super(Options, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)  # For helptext
        self.setMinimumWidth(px(570))
        self.setMaximumWidth(px(570))
        self.setMinimumHeight(px(395))

        # Infer name from command
        command_name = " ".join(command.__name__.split("_")).title()
        self.setWindowTitle(command_name + " options")

        panels = {
            "Central": QtWidgets.QWidget(),
            "Body": QtWidgets.QStackedWidget(),
            "Buttons": QtWidgets.QWidget(),
            "Footer": QtWidgets.QWidget(),
            "Timeline": QtWidgets.QWidget(),
        }

        _Button = _with_entered_exited_signals(QtWidgets.QPushButton)

        widgets = {
            "Confirm": _Button(command_name),
            "Apply": _Button("Apply"),
            "Close": _Button("Close"),
            "Hint": QtWidgets.QLabel(),
            "PlayerButton": _Button(),
            "TimelineButton": _Button(),
            "TimelineSpacing": QtWidgets.QWidget(),
            "Player": Player(media, self),
            "Help": HelpPage(),
            "Options": QtWidgets.QWidget(),
            "Timeline": Timeline(media),
            "Background": QtWidgets.QLabel(),
            "Foreground": QtWidgets.QLabel(),

            "Parser": qargparse.QArgumentParser(args, style={
                "comboboxFillWidth": False,

                # We'll defer these to the footer
                "useTooltip": False,
            }),
        }

        effects = {
            "Foreground": QtWidgets.QGraphicsOpacityEffect(
                widgets["Foreground"]),
        }

        keys = {
            "Escape": QtWidgets.QShortcut(self)
        }

        for name, obj in widgets.items():
            obj.setObjectName(name)
            obj.setAttribute(QtCore.Qt.WA_StyledBackground)

        for name, obj in panels.items():
            obj.setObjectName(name)

        player_icon = QtGui.QPixmap(_resource("icons", "play.png"))
        widgets["PlayerButton"].setCheckable(True)
        widgets["PlayerButton"].setIcon(player_icon)
        widgets["PlayerButton"].setIconSize(QtCore.QSize(px(20), px(20)))

        pause_icon = QtGui.QPixmap(_resource("icons", "pause.png"))
        widgets["TimelineButton"].setIcon(pause_icon)
        widgets["TimelineButton"].setIconSize(QtCore.QSize(px(20), px(20)))
        widgets["TimelineButton"].setCheckable(True)
        widgets["TimelineButton"].setChecked(True)

        helptext = "Hover over an option to read more about it"
        widgets["Hint"].setProperty("defaultText", helptext)
        widgets["Hint"].setText(helptext)

        widgets["Confirm"].clicked.connect(self.on_confirm_clicked)
        widgets["Apply"].clicked.connect(self.on_apply_clicked)
        widgets["Close"].clicked.connect(self.on_close_clicked)

        pixmap = QtGui.QPixmap(_resource("ui", "option_header.png"))
        pixmap = pixmap.scaledToWidth(px(570), QtCore.Qt.SmoothTransformation)
        widgets["Background"].setPixmap(pixmap)
        widgets["Background"].setFixedHeight(px(75))
        widgets["Background"].setFixedWidth(px(570))

        panels["Timeline"].setFixedHeight(px(75))
        panels["Footer"].setFixedHeight(px(75))

        widgets["Background"].setParent(self)

        widgets["Hint"].setParent(panels["Footer"])
        widgets["Hint"].move(px(80), px(9))
        widgets["Hint"].setFixedWidth(px(350))
        widgets["Hint"].setFixedHeight(px(75))
        widgets["Hint"].setWordWrap(True)
        widgets["Hint"].setAlignment(QtCore.Qt.AlignTop)

        widgets["PlayerButton"].setParent(panels["Footer"])
        widgets["PlayerButton"].move(px(10), px(10))
        widgets["PlayerButton"].setFixedSize(px(55), px(55))
        widgets["PlayerButton"].clicked.connect(self.on_play)
        widgets["PlayerButton"].hide()

        # widgets["TimelineButton"].setFixedSize(px(55), px(55))
        widgets["TimelineButton"].clicked.connect(self.on_back)

        widgets["Timeline"].pressed.connect(widgets["Player"].pause)
        widgets["Timeline"].moved.connect(widgets["Player"].jump_to)
        widgets["Timeline"].released.connect(widgets["Player"].play)
        widgets["Player"].frame_changed.connect(widgets["Timeline"].jump_to)

        def add_help(button, text):
            def on_entered():
                widgets["Hint"].setText(text)

            def on_exited():
                self.on_exited()

            button.entered.connect(on_entered)
            button.exited.connect(on_exited)

        add_help(widgets["PlayerButton"], "Click for demonstration video")
        add_help(widgets["Confirm"], "Apply and close")
        add_help(widgets["Apply"], "Apply and keep window open")
        add_help(widgets["Close"], "Do nothing and close")

        widgets["Parser"].changed.connect(self.on_changed)
        widgets["Parser"].entered.connect(self.on_entered)
        widgets["Parser"].exited.connect(self.on_exited)

        widgets["Help"].load(key)
        widgets["Help"].back.connect(self.on_back)

        menu = self.menuBar()
        edit = menu.addMenu("&Edit")
        hlp = menu.addMenu("&Help")

        save = edit.addAction("Save Settings")
        save.triggered.connect(self.on_save)
        reset = edit.addAction("Reset Settings")
        reset.triggered.connect(self.on_reset)
        hlp_on = hlp.addAction("Help on %s" % command_name)
        hlp_on.triggered.connect(self.on_help)

        panels["Body"].addWidget(widgets["Options"])
        panels["Body"].addWidget(widgets["Player"])
        panels["Body"].addWidget(widgets["Help"])

        self.OptionsPage = 0
        self.PlayerPage = 1
        self.HelpPage = 2

        widgets["Parser"].help_wanted.connect(self.on_help)
        widgets["Parser"].help_entered.connect(self.on_help_entered)
        widgets["Parser"].help_exited.connect(self.on_exited)
        widgets["Parser"].setIcon(icon)
        widgets["Parser"].setDescription(description)

        layout = QtWidgets.QVBoxLayout(widgets["Options"])
        layout.addWidget(widgets["Parser"], 1)
        layout.setContentsMargins(px(50), px(5), px(5), px(5))

        layout = QtWidgets.QHBoxLayout(panels["Buttons"])
        layout.setContentsMargins(px(5), px(5), px(5), px(7))
        layout.addWidget(widgets["Confirm"])
        layout.addWidget(widgets["Apply"])
        layout.addWidget(widgets["Close"])

        layout = QtWidgets.QVBoxLayout(panels["Central"])
        layout.addWidget(panels["Body"])
        layout.addWidget(panels["Buttons"])
        layout.addWidget(panels["Footer"])
        layout.addWidget(panels["Timeline"])
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout = QtWidgets.QHBoxLayout(panels["Timeline"])
        layout.addWidget(widgets["TimelineButton"])
        layout.addWidget(widgets["Timeline"])
        layout.addWidget(widgets["TimelineSpacing"])
        layout.setContentsMargins(0, 0, 0, 0)

        # Shown during playback
        panels["Timeline"].hide()

        widgets["Foreground"].setAttribute(QtCore.Qt.WA_StyledBackground)
        widgets["Foreground"].setGraphicsEffect(effects["Foreground"])
        widgets["Foreground"].setParent(self)
        widgets["Foreground"].hide()

        keys["Escape"].setKey(QtGui.QKeySequence("Esc"))
        keys["Escape"].activated.connect(self.on_back)
        keys["Escape"].setEnabled(False)

        self._keys = keys
        self._panels = panels
        self._widgets = widgets
        self._effects = effects
        self._command = command
        self._media = media

        self.setStyleSheet(_scaled_stylesheet(stylesheet))
        self.setCentralWidget(panels["Central"])

    def showEvent(self, event):
        super(Options, self).showEvent(event)

        self._widgets["Hint"].show()

        if self._media:
            self._widgets["PlayerButton"].show()

    def resizeEvent(self, event):
        height = event.size().height()
        width = event.size().width()
        self._widgets["Background"].move(0, height - px(75))
        self._widgets["Background"].resize(width, px(75))
        self._widgets["Foreground"].resize(event.size())
        return super(Options, self).resizeEvent(event)

    @property
    def parser(self):
        return self._widgets["Parser"]

    def transition(self, to):
        """Animate the transition from whatever state we're in, to `to`

        The transition is a solid color in front of all widgets that fade in,
        then a switch is made, followed by a fade out.

        """

        effect = self._effects["Foreground"]
        effect.setOpacity(0.0)

        def rehide():
            # Take case to physically hide the foreground widget,
            # as it would otherwise swallow all mouse and keyboard events
            self._widgets["Foreground"].hide()

        def fade_in():
            to()

            anim = QtCore.QPropertyAnimation(effect, b"opacity")
            anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
            anim.setDuration(100)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.start()

            anim.finished.connect(rehide)

            # Keep from garbage collector
            self.__fadeout = anim

        def fade_out():
            self._widgets["Foreground"].show()
            self._widgets["Foreground"].raise_()

            anim = QtCore.QPropertyAnimation(effect, b"opacity")
            anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
            anim.setDuration(100)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.start()

            anim.finished.connect(fade_in)

            # Keep from garbage collector
            self.__fadeout = anim

        fade_out()

    def on_help(self):
        self.transition(to=self.help_state)

    def on_back(self):
        self.transition(to=self.options_state)

    def on_play(self):
        self.transition(to=self.play_state)

    def options_state(self):
        self._panels["Body"].setCurrentIndex(self.OptionsPage)
        self._widgets["PlayerButton"].setChecked(False)
        self._widgets["Player"].stop()
        self._panels["Buttons"].show()
        self._panels["Footer"].show()
        self._panels["Timeline"].hide()
        self.menuBar().show()
        icon = _resource("icons", "play.png")
        self._widgets["PlayerButton"].setIcon(QtGui.QPixmap(icon))
        self._keys["Escape"].setEnabled(False)

    def play_state(self):
        self._widgets["TimelineButton"].setChecked(True)
        self._panels["Body"].setCurrentIndex(self.PlayerPage)
        self._widgets["Player"].start()
        self._panels["Buttons"].hide()
        self._panels["Footer"].hide()
        self._panels["Timeline"].show()
        self.menuBar().hide()

        icon = _resource("icons", "pause.png")
        self._widgets["PlayerButton"].setIcon(QtGui.QPixmap(icon))
        self._keys["Escape"].setEnabled(True)

    def help_state(self):
        self.menuBar().hide()
        self._panels["Buttons"].hide()
        self._panels["Footer"].hide()
        self._panels["Body"].setCurrentIndex(self.HelpPage)
        self._keys["Escape"].setEnabled(True)

    def on_confirm_clicked(self):
        if self._command():
            self.close()

    def on_apply_clicked(self):
        self._command()

    def on_close_clicked(self):
        self.close()

    def on_reset(self):
        for arg in self._widgets["Parser"]:
            arg.reset()

    def on_save(self):
        # They auto-save, so this is mostly for show
        # Like the button on pedestrian crossings
        log.info("Saved")

    def on_changed(self, arg):
        key = arg["name"]
        value = arg.read()

        options.write(key, value)

        log.debug("Edited %s=%s" % (key, value))

    def on_help_entered(self):
        self._widgets["Hint"].setText(
            "Click for an extended help page about %s"
            % self.windowTitle()
        )

    def on_entered(self, arg):
        text = markdown.markdown(arg["help"])
        self._widgets["Hint"].setText(text)

    def on_exited(self, arg=None):
        default = self._widgets["Hint"].property("defaultText")
        self._widgets["Hint"].setText(default)


YesButton = QtWidgets.QMessageBox.Yes
NoButton = QtWidgets.QMessageBox.No
CancelButton = QtWidgets.QMessageBox.Cancel
OkButton = QtWidgets.QMessageBox.Ok


def MessageBox(title, text, buttons=None):
    parent = MayaWindow()
    message = QtWidgets.QMessageBox(parent)

    buttons = buttons or (
        QtWidgets.QMessageBox.Yes |
        QtWidgets.QMessageBox.No
    )

    message.setStandardButtons(buttons)

    message.setWindowTitle(title)
    message.setText(text)

    return message.exec_() == QtWidgets.QMessageBox.Yes


class SplashScreen(QtWidgets.QDialog):
    """Ragdoll Splash
     ____________________________________________
    |                                          x |
    |                                            |
    |                                            |
    |                                            |
    |                                            |
    |                    Logo                    |
    |                                            |
    |                                            |
    |                                            |
    |                                            |
    |____________________________________________|
    |                                            |
    | Licence text                   Expiry date |
    | __________________________________________ |
    ||                                          ||
    || Serial number 0000-0000-0000-0000        ||
    ||__________________________________________||
    | ___________  ______________  _____________ |
    ||           ||              ||             ||
    ||  Activate ||   Offline    ||   Trial     ||
    ||___________||______________||_____________||
    |____________________________________________|

    """

    show_again_toggled = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super(SplashScreen, self).__init__(parent)
        self.setWindowTitle("Activate Ragdoll Dynamics")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowFlags(QtCore.Qt.Tool)
        # self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        # self.setWindowFlags(QtCore.Qt.FramelessWindowHint)
        self.setFixedSize(px(500), px(410))  # 0.5x the size of splash.png

        panels = {
            "body": QtWidgets.QWidget(self),
            "status": QtWidgets.QWidget(),
            "serial": QtWidgets.QWidget(),
            "buttons": QtWidgets.QWidget(),
        }

        widgets = {
            "background": QtWidgets.QLabel(),
            "close": QtWidgets.QPushButton(self),
            "statusActivated": QtWidgets.QLabel(),
            "statusVerified": QtWidgets.QLabel(),
            "statusMessage": QtWidgets.QLabel("Trial"),
            "expiryDate": QtWidgets.QLabel(),
            "serial": QtWidgets.QLineEdit(),
            "activate": QtWidgets.QPushButton("Activate"),
            "deactivate": QtWidgets.QPushButton("Deactivate"),
            "activateOffline": QtWidgets.QPushButton("Activate Offline"),
            "continue": QtWidgets.QPushButton("Continue"),
        }

        for name, obj in widgets.items():
            obj.setObjectName(name)

        for name, obj in panels.items():
            obj.setObjectName(name)

        pixmap = QtGui.QPixmap(_resource("ui", "splash3.png"))
        pixmap = pixmap.scaledToWidth(px(500), QtCore.Qt.SmoothTransformation)
        widgets["background"].setPixmap(pixmap)

        pixmap = QtGui.QPixmap(_resource("ui", "mode_red.png"))
        pixmap = pixmap.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)
        widgets["statusActivated"].setPixmap(pixmap)

        pixmap = QtGui.QPixmap(_resource("ui", "mode_orange.png"))
        pixmap = pixmap.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)
        widgets["statusVerified"].setPixmap(pixmap)

        icon = widgets["statusVerified"].setToolTip(
            "Could not verify with licence server")

        icon = QtGui.QIcon(QtGui.QPixmap(_resource("ui", "close_button.png")))
        widgets["close"].setIcon(icon)
        widgets["close"].setIconSize(QtCore.QSize(px(20), px(20)))

        panels["body"].setAttribute(QtCore.Qt.WA_StyledBackground)
        panels["body"].setFixedHeight(px(120))
        panels["body"].setObjectName("body")  # For CSS

        widgets["serial"].setPlaceholderText(
            "Enter your key, e.g. 0000-0000-0000-0000-0000-0000-0000"
        )

        layout = QtWidgets.QHBoxLayout(panels["status"])
        layout.addWidget(widgets["statusActivated"])
        # layout.addWidget(widgets["statusVerified"])
        layout.addWidget(widgets["statusMessage"])
        layout.addWidget(QtWidgets.QWidget(), 1)
        layout.addWidget(widgets["expiryDate"])
        layout.setSpacing(px(5))
        layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QHBoxLayout(panels["serial"])
        layout.addWidget(widgets["serial"])
        layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QHBoxLayout(panels["buttons"])
        layout.addWidget(widgets["activate"])
        layout.addWidget(widgets["deactivate"])
        layout.addWidget(widgets["activateOffline"])
        layout.addWidget(widgets["continue"])
        layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(panels["body"])
        layout.addWidget(panels["status"])
        layout.addWidget(panels["serial"])
        layout.addWidget(panels["buttons"])
        layout.addWidget(QtWidgets.QWidget(), 1)
        layout.setSpacing(px(10))
        layout.setContentsMargins(px(10), px(10), px(10), px(10))

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(widgets["background"])
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        widgets["serial"].textChanged.connect(self.on_serial_changed)
        widgets["activate"].clicked.connect(self.on_activate_clicked)
        widgets["deactivate"].clicked.connect(self.on_deactivate_clicked)
        widgets["continue"].clicked.connect(self.on_continue_clicked)
        widgets["activateOffline"].setEnabled(False)
        widgets["deactivate"].hide()

        # Free-floating
        panels["body"].show()
        panels["body"].raise_()
        # widgets["close"].show()
        # widgets["close"].raise_()

        widgets["close"].clicked.connect(self.close)

        anim = QtCore.QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(150)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        self.setStyleSheet(_scaled_stylesheet(stylesheet))

        self._fade_anim = anim
        self._panels = panels
        self._widgets = widgets
        self._data = {}

        self.animate_fade_in()

        self.refresh()
        self.on_serial_changed()
        self.activateWindow()

    def refresh(self):
        data = licence.data()

        # Reset expiry
        self._widgets["expiryDate"].setText("")

        if data["key"]:
            self._widgets["serial"].setText("")
            self._widgets["serial"].setPlaceholderText(data["key"])

        if data["isActivated"]:
            self.on_activated()

        elif data["isTrial"]:
            if data["trialDays"] > 0:
                self.on_trial(data["trialDays"])

            elif data["trialDays"] < 1:
                if data["magicDays"] > 0:
                    self.on_magic(data["magicDays"])
                else:
                    self.on_expired(data["trialDays"])

            elif data["isVerified"]:
                self.on_verified()

        elif data["magicDays"] > 0:
            # It's in the air
            self.on_magic(data["magicDays"])

        else:
            self.on_deactivated()

        self._data = data

    def on_serial_changed(self):
        key = self._widgets["serial"].text()

        if not key:
            key = self._data.get("key")

        self._widgets["activate"].setEnabled(bool(key))

    def on_continue_clicked(self):
        self.close()

    def on_activate_clicked(self):
        key = self._widgets["serial"].text()

        if not key:
            key = self._data.get("key")

        if not key:
            return log.warning("Provide a product key")

        if len(key) != 34 or key.count("-") != 6:
            return log.warning(
                "Bad product key, ensure it is in the following format: "
                "0000-0000-0000-0000-0000-0000-0000"
            )

        status = licence.activate(key)

        if status == licence.STATUS_OK:
            self.refresh()

    def on_deactivate_clicked(self):
        msgbox = QtWidgets.QMessageBox()
        msgbox.setWindowTitle("Deactivate Ragdoll")
        msgbox.setText(
            "Do you want me to deactivate Ragdoll on this machine?\n\n"
            "I'll try and revert to a trial licence."
        )
        msgbox.setStandardButtons(QtWidgets.QMessageBox.Yes |
                                  QtWidgets.QMessageBox.No)

        if msgbox.exec_() != QtWidgets.QMessageBox.Yes:
            return log.info("Cancelled")

        status = licence.deactivate()

        if status == licence.STATUS_OK:
            pass

        elif status == licence.STATUS_ALREADY_ACTIVATED:
            pass

        else:
            pass

        self.refresh()

    def _expiry_string(self, trial_days):
        datestring = datetime.datetime.now()
        datestring += datetime.timedelta(days=trial_days)
        datestring = datestring.strftime("%d %B %Y")
        return datestring

    def on_trial(self, trial_days):
        log.info("Ragdoll is in trial mode")

        status = _resource("ui", "mode_orange.png")
        status = QtGui.QPixmap(status)
        status = status.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)

        icon = self._widgets["statusActivated"]
        icon.setPixmap(status)
        icon.setToolTip("Licence is in trial mode")

        message = self._widgets["statusMessage"]
        message.setText(
            "Ragdoll - %s - Trial Version (node-locked)"
            % __.version_str
        )

        datestring = self._expiry_string(trial_days)
        expiry = self._widgets["expiryDate"]
        expiry.setText("Expires %s" % datestring)

    def on_magic(self, magic_days):
        self.on_deactivated()

        log.info("Ragdoll is magic")

        status = _resource("ui", "mode_orange.png")
        status = QtGui.QPixmap(status)
        status = status.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)

        icon = self._widgets["statusActivated"]
        icon.setPixmap(status)
        icon.setToolTip("Licence is in magic mode")

        message = self._widgets["statusMessage"]
        message.setText(
            "Ragdoll - %s - Magic Version"
            % __.version_str
        )

        datestring = self._expiry_string(magic_days)
        expiry = self._widgets["expiryDate"]
        expiry.setText("Expires %s" % datestring)

    def on_expired(self, trial_days):
        log.info("Ragdoll is expired")

        self._widgets["activate"].show()
        self._widgets["activate"].setEnabled(False)
        self._widgets["deactivate"].hide()
        self._widgets["serial"].setText("")
        self._widgets["serial"].setPlaceholderText(
            "Enter your key, e.g. 0000-0000-0000-0000-0000-0000-0000"
        )

        status = _resource("ui", "mode_red.png")
        status = QtGui.QPixmap(status)
        status = status.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)

        icon = self._widgets["statusActivated"]
        icon.setPixmap(status)
        icon.setToolTip("Trial has expired.")

        message = self._widgets["statusMessage"]
        message.setText("Trial has <b>expired</b>")

        datestring = self._expiry_string(trial_days)
        expiry = self._widgets["expiryDate"]
        expiry.setText("Expired %s" % datestring)

    def on_activated(self):
        log.info("Ragdoll is activated")

        self._widgets["activate"].hide()
        self._widgets["deactivate"].show()

        status = _resource("ui", "mode_green.png")
        status = QtGui.QPixmap(status)
        status = status.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)

        icon = self._widgets["statusActivated"]
        icon.setPixmap(status)
        icon.setToolTip("Enterprise licence is active")

        message = self._widgets["statusMessage"]
        message.setText(
            "Ragdoll - %s - Enterprise Edition (node-locked)"
            % __.version_str
        )

        offline = self._widgets["activateOffline"]
        offline.setText("Deactivate Offline")

    def on_deactivated(self):
        log.info("Ragdoll is deactivated")

        self._widgets["activate"].show()
        self._widgets["deactivate"].hide()
        self._widgets["serial"].setPlaceholderText(
            "Enter your key, e.g. 0000-0000-0000-0000-0000-0000-0000"
        )

        offline = self._widgets["activateOffline"]
        offline.setText("Activate Offline")

    def on_verified(self):
        status = _resource("ui", "mode_green.png")
        status = QtGui.QPixmap(status)
        status = status.scaledToWidth(px(15), QtCore.Qt.SmoothTransformation)

        icon = self._widgets["statusVerified"]
        icon.setPixmap(status)
        icon.setToolTip("Successfully verified with licence server")

    def resizeEvent(self, event):
        # Keep body at bottom, over backdrop
        size = event.size()
        body = self._panels["body"]
        height = body.height()
        body.setFixedWidth(size.width())
        body.move(0, size.height() - height)

        # Move close button to top-right corner
        close = self._widgets["close"]
        close.move(event.size().width() - px(20), 0)

        return super(SplashScreen, self).resizeEvent(event)

    def animate_fade_in(self):
        self._fade_anim.start()


def center_window(window):
    # Center in the Maya window
    maya_window = MayaWindow()

    center = maya_window.size() / 2
    center -= window.size() / 2.0

    if center.width() > 0 and center.height() > 0:
        pos = maya_window.pos()
        pos += QtCore.QPoint(center.width(), center.height())
        window.move(pos)

    else:
        log.debug(
            "Failed. Couldn't figure out the Maya window size"
        )


def warn(option, title, message, call_to_action, actions):
    msgbox = QtWidgets.QMessageBox()
    msgbox.setWindowTitle(title)
    msgbox.setText(message)
    msgbox.setInformativeText(call_to_action)

    buttons = []
    for index, (label, func) in enumerate(actions):
        button = msgbox.addButton(label, msgbox.ActionRole)
        buttons += [button]

    def on_remind_me(value):
        options.write(option, bool(value))
        log.info("Toggled %s" % option)

    remind_me = QtWidgets.QCheckBox("Keep telling me")
    remind_me.setChecked(True)
    remind_me.stateChanged.connect(on_remind_me)

    layout = msgbox.layout()
    layout.addWidget(remind_me, 3, 1, QtCore.Qt.AlignRight)

    msgbox.exec_()

    clicked = msgbox.clickedButton()
    for index, button in enumerate(buttons):
        if button == clicked:
            _, func = actions[index]
            return func()

    return False


class MessageBoard(QtWidgets.QDialog):
    def __init__(self, records, parent=None):
        super(MessageBoard, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowTitle("Ragdoll Message Board")
        self.setMinimumWidth(px(600))
        self.setMinimumHeight(px(300))

        panels = {
            "body": QtWidgets.QWidget(),
            "footer": QtWidgets.QWidget(),
        }

        widgets = {
            "board": QtWidgets.QListWidget(),
            "close": QtWidgets.QPushButton("Close"),
        }

        count = 1
        for index, record in enumerate(records):
            if index < len(records) - 1:
                next_record = records[index + 1]

                if next_record.msg == record.msg:
                    count += 1
                    continue

            msg = "%s" % record.msg

            if count > 1:
                msg += " (%d)" % count

            widgets["board"].addItem(msg)
            count = 1

        layout = QtWidgets.QVBoxLayout(panels["body"])
        layout.addWidget(widgets["board"])

        layout = QtWidgets.QHBoxLayout(panels["footer"])
        layout.addWidget(widgets["close"])

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(panels["body"])
        layout.addWidget(panels["footer"])
        layout.setContentsMargins(0, 0, 0, 0)

        widgets["close"].clicked.connect(self.close)

        self._panels = panels
        self._widgets = widgets


class Explorer(MayaQWidgetDockableMixin, QtWidgets.QDialog):
    instance = None

    def __init__(self, parent=None):
        super(Explorer, self).__init__(parent)
        self.setWindowTitle("Ragdoll Explorer")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setMinimumWidth(px(200))
        self.setMinimumHeight(px(100))

        model = qjsonmodel.QJsonModel(editable=False)

        view = QtWidgets.QTreeView()
        view.setModel(model)
        view.header().resizeSection(0, px(200))

        raw = QtWidgets.QCheckBox("Show Raw")
        raw.stateChanged.connect(self.on_raw_changed)

        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.reload)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(view)
        layout.addWidget(raw)
        layout.addWidget(refresh)

        timer = QtCore.QTimer(parent=self)  # Delete on close
        timer.setInterval(1000.0)  # ms
        timer.timeout.connect(self.reload)

        self._view = view
        self._model = model
        self._timer = timer
        self._dump = None
        self._raw = False

        Explorer.instance = self

        # For uninstall
        __.widgets[self.windowTitle()] = self

    def on_raw_changed(self, state):
        self._raw = bool(state)
        self.load(self._dump)

    def parse(self, dump, raw=False):
        if self._raw:
            dump["entities"] = {
                dump.Entity(entity): value
                for entity, value in dump["entities"].items()
            }
        else:
            for key in dump["entities"].copy():
                value = dump["entities"].pop(key)

                if "NameComponent" not in value["components"]:
                    continue

                name = value["components"]["NameComponent"]

                # E.g. _:|root_grp|upperArm_ctl|rRigid1
                name = "|".join([name["members"]["shortestPath"],
                                 name["members"]["value"]])

                # E.g. upperArm_ctl|rRigid1
                name = name.rsplit(":", 1)[-1]

                # This should never really happen, it means
                # there are two entities for a Ragdoll node.
                index = 1
                orig = name
                while name in dump["entities"]:
                    name = "%s (%d!)" % (orig, index)
                    index += 1

                # Move members directly under the component
                dump["entities"][name] = {
                    k: v["members"]
                    for k, v in value["components"].items()
                }

        return dump

    def load(self, dump):
        self._dump = dump
        self.reload()

        # Need to implement updating of individual fields,
        # in order to not loose selection and navigation
        # self._timer.start()

    def reload(self):
        assert self._dump is not None, "Call load() first"
        dump = self._dump

        if callable(dump):
            dump = dump()

        if isinstance(dump, i__.string_types):
            dump = json.loads(dump)

        assert isinstance(dump, dict)

        parsed = self.parse(dump)
        self._model.load(parsed)
        self._view.expandToDepth(0)


EntityRole = QtCore.Qt.UserRole + 0
TransformRole = QtCore.Qt.UserRole + 1
OptionsRole = QtCore.Qt.UserRole + 2
OccupiedRole = QtCore.Qt.UserRole + 3
HintRole = QtCore.Qt.UserRole + 4
PathRole = QtCore.Qt.UserRole + 5


Load = "Load"
Reinterpret = "Reinterpret"


class DumpView(QtWidgets.QTreeView):
    mouseMoved = QtCore.Signal(QtCore.QPoint)  # Pos

    def __init__(self, parent=None):
        super(DumpView, self).__init__(parent)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit(event.pos())
        return super(DumpView, self).mouseMoveEvent(event)


class DumpWidget(QtWidgets.QWidget):
    hinted = QtCore.Signal(str)  # Hint

    def __init__(self, loader, parent=None):
        super(DumpWidget, self).__init__(parent)

        panels = {
            "Body": QtWidgets.QWidget(),
        }

        widgets = {
            "TargetView": DumpView(),
        }

        models = {
            "TargetModel": qargparse.GenericTreeModel(),
        }

        for name, wid in panels.items():
            wid.setObjectName(name)

        for name, wid in widgets.items():
            wid.setObjectName(name)

        # Setup

        widgets["TargetView"].mouseMoved.connect(self.on_mouse_moved)

        widgets["TargetView"].setModel(models["TargetModel"])
        widgets["TargetView"].setSelectionBehavior(
            QtWidgets.QTreeView.SelectRows)

        # Layout

        layout = QtWidgets.QHBoxLayout(self)
        layout.addWidget(widgets["TargetView"])
        layout.setContentsMargins(0, 0, 0, 0)

        selection_model = widgets["TargetView"].selectionModel()
        selection_model.currentRowChanged.connect(self.on_target_changed)

        timer = QtCore.QTimer(parent=self)
        timer.setInterval(50.0)  # ms
        timer.setSingleShot(True)
        timer.timeout.connect(self._reset)

        self._reset_timer = timer
        self._widgets = widgets
        self._panels = panels
        self._models = models
        self._loader = loader

        self.setStyleSheet(_scaled_stylesheet("""
            QTreeView {
                outline: none;
            }

            QTreeView::item {
                border: 0px;
                padding-left: 0px;
                padding-right: 10px;
            }

            QTreeView::item:selected {
                background: #5285a6;
            }
        """))

    def leaveEvent(self, event):
        self.hinted.emit("")  # Clear
        return super(DumpWidget, self).leaveEvent(event)

    def on_mouse_moved(self, pos):
        index = self._widgets["TargetView"].indexAt(pos)
        tooltip = index.data(HintRole)
        self.hinted.emit(tooltip or "")

    def on_target_changed(self, current, previous):
        pass

    def reset(self):
        # Reset after a given time period.
        # This allows reset to get called frequently, without
        # actually incuring the cost of resetting the model each
        # time. It keeps the user interactivity swift and clean.
        self._reset_timer.start()

    def _reset(self):
        def _transform(data, entity):
            Name = self._loader.component(entity, "NameComponent")

            alignment = (
                QtCore.Qt.AlignLeft,
                QtCore.Qt.AlignLeft,
                QtCore.Qt.AlignLeft,
            )

            shape_icon = None
            rigid_entity = entity

            # Borrow a shape icon from whatever rigid this constraint applies
            if self._loader.has(entity, "JointComponent"):
                Joint = self._loader.component(entity, "JointComponent")

                # Protect against possible (but improbable) invalid reference
                if Joint["child"]:
                    rigid_entity = Joint["child"]

            if self._loader.has(rigid_entity, "RigidUIComponent"):
                rigid_ui = self._loader.component(rigid_entity,
                                                  "RigidUIComponent")

                # TODO: Replace .get with []
                shape_icon = rigid_ui.get("shapeIcon")
                shape_icon = {
                    "joint": "maya_joint.png",
                    "mesh": "maya_mesh.png",
                    "nurbsCurve": "maya_curve.png",
                    "nurbsSurface": "maya_surface.png",
                }.get(shape_icon, "maya_transform.png")

                shape_icon = _resource("icons", shape_icon)
                shape_icon = QtGui.QIcon(shape_icon)

            try:
                transform = analysis["transforms"][entity]

            except KeyError:

                # Dim any transform that isn't getting imported
                color = self.palette().color(self.foregroundRole())
                color.setAlpha(100)
                grayed_out = (color, color, color)

                try:
                    # Is there a transform, except it's oppupied?
                    occupied = analysis["occupied"][entity]

                except KeyError:
                    # No, there isn't a transform for this entity
                    icon = _resource("icons", "questionmark.png")
                    icon = QtGui.QIcon(icon)
                    tooltip = "No transform could be found for this rigid"

                    data[QtCore.Qt.DecorationRole] += [shape_icon, icon]
                    data[QtCore.Qt.TextAlignmentRole] = alignment
                    data[QtCore.Qt.ForegroundRole] = grayed_out

                    data[OccupiedRole] = False
                    data[HintRole] += (
                        Name["path"],
                        tooltip
                    )

                else:
                    icon = _resource("icons", "check.png")
                    icon = QtGui.QIcon(icon)
                    path = occupied.shortestPath()

                    data[QtCore.Qt.DisplayRole] += [path]
                    data[QtCore.Qt.DecorationRole] += [shape_icon, icon]
                    data[QtCore.Qt.ForegroundRole] = (color, color)
                    data[QtCore.Qt.TextAlignmentRole] = alignment

                    data[TransformRole] = occupied
                    data[OccupiedRole] = True
                    data[HintRole] += (
                        occupied.path(),
                        "%s already has a rigid" % path
                    )
            else:
                icon = _resource("icons", "right.png")
                icon = QtGui.QIcon(icon)

                data[QtCore.Qt.DecorationRole] += [shape_icon, icon]
                data[QtCore.Qt.DisplayRole] += [transform.shortestPath()]
                data[QtCore.Qt.TextAlignmentRole] = alignment

                data[TransformRole] = transform
                data[HintRole] += (
                    Name["path"],
                    transform.path()
                )

        def _rigid_icon(data, entity):
            Desc = self._loader.component(
                entity, "GeometryDescriptionComponent"
            )

            icon = {
                "Box": "box.png",
                "Capsule": "capsule.png",
                "Cylinder": "cylinder.png",
                "Sphere": "sphere.png",
                "ConvexHull": "mesh.png",
            }.get(
                Desc["type"],

                # In case the format is all whack
                "rigid.png"
            )

            icon = _resource("icons", icon)
            icon = QtGui.QIcon(icon)

            data[QtCore.Qt.DecorationRole] += [icon]

        def _default_data():
            return {
                QtCore.Qt.DisplayRole: [],
                QtCore.Qt.DecorationRole: [],
                QtCore.Qt.TextAlignmentRole: [],

                HintRole: [],
                TransformRole: None,
                EntityRole: None,
                OptionsRole: None,
            }

        def _add_scenes(root_item):
            for scene in analysis["scenes"]:
                entity = scene["entity"]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "scene.png")
                icon = QtGui.QIcon(icon)

                transform_data = _default_data()
                transform_data[QtCore.Qt.DisplayRole] += ["Scene", label]
                transform_data[QtCore.Qt.DecorationRole] += [icon]
                transform_data[HintRole] += ["Scene Command"]
                transform_data[EntityRole] = entity
                transform_data[OptionsRole] = scene["options"]

                _transform(transform_data, entity)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += [Name["value"],
                                                Name.get("shortestPath", "")]
                data[EntityRole] = entity

                _rigid_icon(data, entity)
                _transform(data, entity)

                transform_item = qargparse.GenericTreeModelItem(transform_data)
                entity_item = qargparse.GenericTreeModelItem(data)

                root_item.addChild(transform_item)
                transform_item.addChild(entity_item)

        def _add_chains(root_item):
            for chain in analysis["chains"]:
                entity = chain["rigids"][0]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "chain.png")
                icon = QtGui.QIcon(icon)

                transform_data = _default_data()
                transform_data[QtCore.Qt.DisplayRole] += ["Chain", label]
                transform_data[QtCore.Qt.DecorationRole] += [icon]
                transform_data[HintRole] += ["Chain Command"]

                transform_data[EntityRole] = entity
                transform_data[OptionsRole] = chain["options"]

                _transform(transform_data, entity)

                chain_item = qargparse.GenericTreeModelItem(transform_data)
                root_item.addChild(chain_item)

                for entity in chain["rigids"]:
                    Name = self._loader.component(entity, "NameComponent")
                    label = Name["path"].rsplit("|", 1)[-1]
                    icon = _resource("icons", "rigid.png")
                    icon = QtGui.QIcon(icon)

                    data = _default_data()
                    data[EntityRole] = entity
                    data[HintRole] += ["Rigid"]
                    data[QtCore.Qt.DisplayRole] += [
                        Name["value"], Name.get("shortestPath", "")
                    ]

                    _rigid_icon(data, entity)
                    _transform(data, entity)

                    chain_item.addChild(qargparse.GenericTreeModelItem(data))

                for entity in chain["constraints"]:
                    icon = _resource("icons", "constraint.png")
                    icon = QtGui.QIcon(icon)

                    Name = self._loader.component(entity, "NameComponent")

                    data = _default_data()
                    data[HintRole] += ["Constraint"]
                    data[QtCore.Qt.DecorationRole] += [icon]
                    data[QtCore.Qt.DisplayRole] += [
                        Name["value"], Name.get("shortestPath", "")
                    ]

                    data[EntityRole] = entity

                    _transform(data, entity)

                    chain_item.addChild(qargparse.GenericTreeModelItem(data))

        def _add_rigids(root_item):
            for rigid in analysis["rigids"]:
                entity = rigid["entity"]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "rigid.png")
                icon = QtGui.QIcon(icon)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += ["Rigid", label]
                data[QtCore.Qt.DecorationRole] += [icon]
                data[HintRole] += ["Rigid Command"]

                data[EntityRole] = entity
                data[OptionsRole] = rigid["options"]

                _transform(data, entity)

                item = qargparse.GenericTreeModelItem(data)
                root_item.addChild(item)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += [Name["value"],
                                                Name.get("shortestPath", "")]
                data[QtCore.Qt.DecorationRole] += []
                data[EntityRole] = entity

                _rigid_icon(data, entity)
                _transform(data, entity)

                child = qargparse.GenericTreeModelItem(data)
                item.addChild(child)

        def _add_constraints(root_item):
            for constraint in analysis["constraints"]:
                entity = constraint["entity"]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "constraint.png")
                icon = QtGui.QIcon(icon)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += ["Constraint", label]
                data[QtCore.Qt.DecorationRole] += [icon]
                data[HintRole] += ["Constraint Command"]
                data[EntityRole] = entity
                data[OptionsRole] = constraint["options"]

                _transform(data, entity)

                item = qargparse.GenericTreeModelItem(data)
                root_item.addChild(item)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += [Name["value"],
                                                Name.get("shortestPath", "")]
                data[EntityRole] = entity
                data[QtCore.Qt.DecorationRole] += [icon]

                _transform(data, entity)

                child = qargparse.GenericTreeModelItem(data)
                item.addChild(child)

        def _add_rigid_multipliers(root_item):
            for mult in analysis["rigidMultipliers"]:
                entity = mult["entity"]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "rigid_multiplier.png")
                icon = QtGui.QIcon(icon)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += ["Rigid Multiplier", label]
                data[QtCore.Qt.DecorationRole] += [icon]
                data[HintRole] += ["Rigid Multiplier Command"]
                data[EntityRole] = entity
                data[OptionsRole] = mult["options"]

                _transform(data, entity)

                item = qargparse.GenericTreeModelItem(data)
                root_item.addChild(item)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += [Name["value"],
                                                Name.get("shortestPath", "")]
                data[EntityRole] = entity
                data[QtCore.Qt.DecorationRole] += [icon]

                _transform(data, entity)

                child = qargparse.GenericTreeModelItem(data)
                item.addChild(child)

        def _add_constraint_multipliers(root_item):
            for mult in analysis["constraintMultipliers"]:
                entity = mult["entity"]

                Name = self._loader.component(entity, "NameComponent")
                label = Name["path"].rsplit("|", 1)[-1]
                icon = _resource("icons", "constraint_multiplier.png")
                icon = QtGui.QIcon(icon)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += ["Constraint Multiplier", label]
                data[QtCore.Qt.DecorationRole] += [icon]
                data[HintRole] += ["Constraint Multiplier Command"]
                data[EntityRole] = entity
                data[OptionsRole] = mult["options"]

                _transform(data, entity)

                item = qargparse.GenericTreeModelItem(data)
                root_item.addChild(item)

                data = _default_data()
                data[QtCore.Qt.DisplayRole] += [Name["value"],
                                                Name.get("shortestPath", "")]
                data[EntityRole] = entity
                data[QtCore.Qt.DecorationRole] += [icon]

                _transform(data, entity)

                child = qargparse.GenericTreeModelItem(data)
                item.addChild(child)

        analysis = self._loader.analyse()
        root_item = qargparse.GenericTreeModelItem({
            QtCore.Qt.DisplayRole: ("Command", "Source Node", "Target Node")
        })

        if not self._loader.is_valid():
            icon = _resource("icons", "error.png")
            icon = QtGui.QIcon(icon)
            invalid_item = qargparse.GenericTreeModelItem({
                QtCore.Qt.DisplayRole: "Invalid .rag file",
                QtCore.Qt.DecorationRole: icon,
                QtCore.Qt.ForegroundRole: QtGui.QColor("#F66"),
            })

            root_item.addChild(invalid_item)

            for reason in self._loader.invalid_reasons():
                log.debug("Failure reason: %s" % reason)

            self._widgets["TargetView"].setIndentation(0)

        else:
            self._widgets["TargetView"].setIndentation(30)

            _add_chains(root_item)
            _add_rigids(root_item)
            _add_constraints(root_item)
            _add_rigid_multipliers(root_item)
            _add_constraint_multipliers(root_item)
            _add_scenes(root_item)

        model = self._models["TargetModel"]
        model.reset(root_item)


class ImportOptions(Options):
    instance = None

    def __init__(self, *args, **kwargs):
        super(ImportOptions, self).__init__(*args, **kwargs)
        self.setWindowTitle("Import Options")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)

        loader = dump.Loader()

        widgets = {
            "DumpWidget": DumpWidget(loader),
            "Thumbnail": QtWidgets.QLabel(),
        }

        # Integrate with other options
        layout = self.parser.layout()
        row = self.parser._row
        alignment = (QtCore.Qt.AlignRight | QtCore.Qt.AlignTop,)

        layout.addWidget(QtWidgets.QLabel("Preview"), row, 0, *alignment)
        layout.addWidget(widgets["DumpWidget"], row, 1)
        layout.setRowStretch(9999, 0)  # Unexpand last row
        layout.setRowStretch(row, 1)  # Expand to fill width

        self.parser._row += 1  # For the next subclass

        # Map known options to this widget
        parser = self._widgets["Parser"]
        import_path = parser.find("importPath")
        import_paths = parser.find("importPaths")
        search_replace = parser.find("importSearchAndReplace")
        use_selection = parser.find("importUseSelection")
        auto_namespace = self.parser.find("importAutoNamespace")

        import_path.changed.connect(self.on_path_changed)
        import_path.browsed.connect(self.on_browsed)
        import_paths.changed.connect(self.on_filename_changed)
        use_selection.changed.connect(self.on_selection_changed)
        auto_namespace.changed.connect(self.on_selection_changed)
        search_replace.changed.connect(self.on_search_and_replace)

        default_thumbnail = _resource("icons", "no_thumbnail.png")
        default_thumbnail = QtGui.QPixmap(default_thumbnail)
        default_thumbnail = default_thumbnail.scaled(
            px(200), px(128),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation
        )

        qimport_paths = import_paths.widget()
        layout = qimport_paths.layout()
        layout.addWidget(widgets["Thumbnail"], 1, QtCore.Qt.AlignCenter)
        widgets["Thumbnail"].setAlignment(QtCore.Qt.AlignCenter)
        widgets["Thumbnail"].setFixedWidth(px(200))
        widgets["Thumbnail"].setFixedHeight(px(128))
        widgets["Thumbnail"].setPixmap(default_thumbnail)
        widgets["Thumbnail"].setStyleSheet("""
            QLabel {
                border: 1px solid #555;
                background: #222;
            }
        """)

        widgets["DumpWidget"].hinted.connect(self.on_hinted)

        self._loader = loader
        self._selection_callback = None
        self._previous_dirname = None
        self._default_thumbnail = default_thumbnail

        # Keep superclass informed
        self._widgets.update(widgets)

        # For uninstall
        ImportOptions.instance = self
        __.widgets[self.windowTitle()] = self

        # Kick things off, a few ms after opening
        QtCore.QTimer.singleShot(200, self.on_path_changed)

    def on_hinted(self, hint):
        if hint:
            text = hint
        else:
            text = self._widgets["Hint"].property("defaultText")

        # Trim very long instances
        if len(text) > 150:
            text = "..." + text[-147:]

        self._widgets["Hint"].setText(text)

    def do_import(self):
        if options.read("importMethod") == Load:
            method = self._loader.load
        else:
            method = self._loader.reinterpret

        def do_it():
            method()
            self.reset()

        # Allow UI to finish drawing the click of a button
        QtCore.QTimer.singleShot(5, do_it)

        return True

    @i__.with_timing
    def reset(self):
        search_replace = self.parser.find("importSearchAndReplace")
        search, replace = search_replace.read()
        self._loader.set_replace([(search, replace)])
        self._widgets["DumpWidget"].reset()

    def load(self, data):
        assert isinstance(data, dict), "data must be a dictionary"

        current_path = self.parser.find("importPath")
        current_path.write("<raw>", notify=False)

        self._loader.read(data)
        self.on_selection_changed()

    def read(self, fname):
        assert isinstance(fname, i__.string_types), "fname must be string"

        current_path = self.parser.find("importPath")
        current_path.write(fname, notify=False)

        self._loader.read(fname)
        self.on_selection_changed()

    def on_selection_changed(self, _=None):
        use_selection = self.parser.find("importUseSelection")
        auto_namespace = self.parser.find("importAutoNamespace")

        if use_selection.read():
            auto_namespace.widget().setEnabled(True)

            roots = cmds.ls(selection=True, type="transform", long=True)

            self._loader.set_namespace(None)
            self._loader.set_roots(roots)

            auto_namespace = self.parser.find("importAutoNamespace")
            if auto_namespace.read():
                namespaces = set()

                for root in roots:
                    namespaces = root.split("|")
                    namespaces = [node.rsplit(":", 1)[0]
                                  for node in namespaces]
                    namespaces = filter(None, namespaces)  # Remove `None`
                    namespaces = tuple(set(namespaces))  # Remove duplicates

                if len(namespaces) > 1:
                    log.debug("Selection had multiple namespaces: %s"
                              % str(namespaces))

                elif len(namespaces) > 0:
                    target_namespace = tuple(namespaces)[0]
                    self._loader.set_namespace(target_namespace)

        else:
            auto_namespace.widget().setEnabled(False)
            self._loader.set_namespace(None)
            self._loader.set_roots([])

        self.reset()

    def on_search_and_replace(self):
        # It'll get fetched during reset
        self.reset()

    def on_path_changed(self):
        SUFFIX = ".rag"

        import_paths = self.parser.find("importPaths")
        import_path = self.parser.find("importPath")

        # Could be empty
        if not import_path.read():
            return

        import_path_str = import_path.read()
        dirname, selected_fname = os.path.split(import_path_str)

        # No need to refresh the directory listing if it's the same directory
        if self._previous_dirname != dirname:
            self._previous_dirname = dirname

            fnames = []
            try:
                for fname in os.listdir(dirname):
                    if fname.endswith(SUFFIX):
                        fnames += [fname]
            except Exception:
                # Whatever is going on, the user can't do anything about it
                pass

            # TODO: Expose choice of icon to the user during export
            icon = _resource("icons", "logo2.png")
            icon = QtGui.QIcon(icon)

            items = []
            fnames = i__.sort_filenames(fnames)
            for fname in fnames:
                item = {
                    PathRole: fname,

                    QtCore.Qt.DecorationRole: icon,
                    QtCore.Qt.DisplayRole: fname,
                }

                items += [item]

            # A folder path was given, no filename
            if not selected_fname:

                # The folder may be empty
                if fnames:
                    selected_fname = fnames[0]

            if not items:
                items += [{
                    PathRole: None,

                    QtCore.Qt.DisplayRole: "Empty folder",
                    QtCore.Qt.DecorationRole: None,
                }]

            import_paths.reset(items,
                               header=("Filename",),
                               current=selected_fname)

        def read():
            self.read(import_path_str)

        # Give UI a chance to keep up
        QtCore.QTimer.singleShot(200, read)

    def on_filename_changed(self):
        current_paths = self.parser.find("importPaths")
        filename = current_paths.read(PathRole)

        if filename is not None:
            # Swap out the filename from the currently loaded path
            current_path = self.parser.find("importPath")
            original_path = current_path.read()

            dirname, _ = os.path.split(original_path)
            path = os.path.join(dirname, filename)

            # Make it official
            current_path.write(path)

        # Clear any current thumbnail
        qthumbnail = self._default_thumbnail

        # Fetch metadata, like description and thumbnail
        try:
            with open(path) as f:
                data = json.load(f)

        except Exception:
            pass

        else:
            ui = data.get("ui", {})
            thumbnail = ui.get("thumbnail")

            if thumbnail:
                # From JSON's unicode
                thumbnail = thumbnail.encode("ascii")

                # To QPixmap
                qthumbnail = base64_to_pixmap(thumbnail)

        self._widgets["Thumbnail"].setPixmap(qthumbnail)

    def on_browsed(self):
        path, suffix = QtWidgets.QFileDialog.getOpenFileName(
            MayaWindow(),
            "Open Ragdoll Scene",
            options.read("lastVisitedPath"),
            "Ragdoll scene files (*.rag)"
        )

        if not path:
            return log.debug("Cancelled")

        path = os.path.normpath(path)
        dirname, fname = os.path.split(path)

        # Update directory listing, even if it's unchanged
        # (in case the contents has actually changed)
        self._previous_dirname = None

        import_path = self.parser.find("importPath")
        import_path.write(path)

    def showEvent(self, event):
        # Keep an eye on the current selection
        self._selection_callback = om.MModelMessage.addCallback(
            om.MModelMessage.kActiveListModified,
            self.on_selection_changed
        )

        super(ImportOptions, self).showEvent(event)

    def closeEvent(self, event):
        if self._selection_callback is not None:
            om.MMessage.removeCallback(self._selection_callback)

        self._selection_callback = None
        super(ImportOptions, self).closeEvent(event)


def view_to_pixmap(size=None):
    """Render currently active 3D viewport as a QPixmap"""

    image = om.MImage()
    view = omui.M3dView.active3dView()
    view.readColorBuffer(image, True)

    # Translate from Maya -> Qt jargon
    image.verticalFlip()

    # Abracadabra!
    osize = size or QtCore.QSize(512, 256)
    isize = image.getSize()
    buf = ctypes.c_ubyte * isize[0] * isize[1]
    buf = buf.from_address(i__.long(image.pixels()))

    # Vimsalabim!
    qimage = QtGui.QImage(
        buf, isize[0], isize[1], QtGui.QImage.Format_RGB32
    ).rgbSwapped()

    return QtGui.QPixmap.fromImage(qimage).scaled(
        osize.width(), osize.height(),
        QtCore.Qt.KeepAspectRatio,
        QtCore.Qt.SmoothTransformation
    )


def pixmap_to_base64(pixmap):
    array = QtCore.QByteArray()
    buffer = QtCore.QBuffer(array)

    buffer.open(QtCore.QIODevice.WriteOnly)
    pixmap.save(buffer, "png")

    return bytes(array.toBase64())


def base64_to_pixmap(base64):
    data = QtCore.QByteArray.fromBase64(base64)
    pixmap = QtGui.QPixmap()
    pixmap.loadFromData(data)
    return pixmap
