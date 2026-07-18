import json
import os
import sqlite3
import sys
from datetime import datetime
from urllib.parse import quote_plus

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QUrl, Qt, Slot
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
)
from PySide6.QtWebEngineWidgets import QWebEngineView


class BrowserDatabase:
    def __init__(self, database_path: str):
        self.connection = sqlite3.connect(database_path)

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                visited_at TEXT NOT NULL
            )
        """)

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        self.connection.commit()

    def add_visit(self, title: str, url: str) -> None:
        if not url:
            return

        last_visit = self.connection.execute("""
            SELECT id, url
            FROM history
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()

        now = datetime.now().isoformat(timespec="seconds")

        if last_visit and last_visit[1] == url:
            self.connection.execute("""
                UPDATE history
                SET title = ?, visited_at = ?
                WHERE id = ?
            """, (title, now, last_visit[0]))
        else:
            self.connection.execute("""
                INSERT INTO history (title, url, visited_at)
                VALUES (?, ?, ?)
            """, (title, url, now))

        self.connection.commit()

    def get_history(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute("""
            SELECT title, url, visited_at
            FROM history
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [
            {"title": title, "url": url, "time": visited_at}
            for title, url, visited_at in rows
        ]

    def clear_history(self) -> None:
        self.connection.execute("DELETE FROM history")
        self.connection.commit()

    def add_bookmark(self, title: str, url: str) -> None:
        self.connection.execute("""
            INSERT INTO bookmarks (title, url, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET title = excluded.title
        """, (
            title or url,
            url,
            datetime.now().isoformat(timespec="seconds"),
        ))
        self.connection.commit()

    def remove_bookmark(self, url: str) -> None:
        self.connection.execute(
            "DELETE FROM bookmarks WHERE url = ?",
            (url,),
        )
        self.connection.commit()

    def is_bookmarked(self, url: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM bookmarks WHERE url = ? LIMIT 1",
            (url,),
        ).fetchone()
        return row is not None

    def get_bookmarks(self) -> list[dict]:
        rows = self.connection.execute("""
            SELECT title, url, created_at
            FROM bookmarks
            ORDER BY id DESC
        """).fetchall()

        return [
            {"title": title, "url": url, "time": created_at}
            for title, url, created_at in rows
        ]

    def close(self) -> None:
        self.connection.close()


class AppState(QObject):
    def __init__(self, project_folder: str):
        super().__init__()

        self.project_folder = project_folder
        self.data_folder = os.path.join(project_folder, "browser_data")
        self.download_folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )

        os.makedirs(self.data_folder, exist_ok=True)
        os.makedirs(self.download_folder, exist_ok=True)

        self.database = BrowserDatabase(
            os.path.join(self.data_folder, "browser.db")
        )

        self.settings = QSettings("Fizz", "Fizz Browser")
        self.windows: list[BrowserWindow] = []

        cache_folder = os.path.join(self.data_folder, "cache")
        storage_folder = os.path.join(self.data_folder, "storage")
        os.makedirs(cache_folder, exist_ok=True)
        os.makedirs(storage_folder, exist_ok=True)

        self.profile = QWebEngineProfile("FizzBrowserProfile", self)
        self.profile.setCachePath(cache_folder)
        self.profile.setPersistentStoragePath(storage_folder)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.profile.downloadRequested.connect(self.handle_download)

    def create_window(self, url: QUrl | None = None) -> "BrowserWindow":
        window = BrowserWindow(self)
        self.windows.append(window)
        window.destroyed.connect(
            lambda: self.remove_window(window)
        )
        window.show()

        if url:
            browser = window.current_browser()
            if browser:
                browser.setUrl(url)

        return window

    def remove_window(self, window: "BrowserWindow") -> None:
        if window in self.windows:
            self.windows.remove(window)

    def handle_download(
        self,
        download: QWebEngineDownloadRequest,
    ) -> None:
        active_window = QApplication.activeWindow()

        suggested_name = download.downloadFileName() or "download"
        default_path = os.path.join(self.download_folder, suggested_name)

        selected_path, _ = QFileDialog.getSaveFileName(
            active_window,
            "Download speichern",
            default_path,
        )

        if not selected_path:
            download.cancel()
            return

        directory, filename = os.path.split(selected_path)
        download.setDownloadDirectory(directory)
        download.setDownloadFileName(filename)
        download.accept()

        if isinstance(active_window, BrowserWindow):
            active_window.statusBar().showMessage(
                f"Download gestartet: {filename}",
                5000,
            )

    def close(self) -> None:
        self.database.close()


class BrowserBridge(QObject):
    def __init__(
        self,
        browser_window: "BrowserWindow",
        browser_view: "BrowserView",
    ):
        super().__init__()
        self.browser_window = browser_window
        self.browser_view = browser_view

    @Slot(str)
    def openWebsite(self, value: str) -> None:
        self.browser_view.setUrl(
            self.browser_window.text_to_url(value)
        )

    @Slot(result=str)
    def getHistory(self) -> str:
        return json.dumps(
            self.browser_window.state.database.get_history(),
            ensure_ascii=False,
        )

    @Slot()
    def clearHistory(self) -> None:
        self.browser_window.state.database.clear_history()

    @Slot(result=str)
    def getBookmarks(self) -> str:
        return json.dumps(
            self.browser_window.state.database.get_bookmarks(),
            ensure_ascii=False,
        )

    @Slot(str, str)
    def addBookmark(self, title: str, url: str) -> None:
        self.browser_window.state.database.add_bookmark(title, url)
        self.browser_window.update_bookmark_button()

    @Slot(str)
    def removeBookmark(self, url: str) -> None:
        self.browser_window.state.database.remove_bookmark(url)
        self.browser_window.update_bookmark_button()

    @Slot()
    def newTab(self) -> None:
        self.browser_window.add_new_tab()

    @Slot()
    def newWindow(self) -> None:
        self.browser_window.state.create_window()

    @Slot()
    def goBack(self) -> None:
        self.browser_view.back()

    @Slot()
    def goForward(self) -> None:
        self.browser_view.forward()

    @Slot()
    def reloadPage(self) -> None:
        self.browser_view.reload()

    @Slot()
    def goHome(self) -> None:
        self.browser_view.setUrl(
            self.browser_window.homepage_url()
        )


class BrowserPage(QWebEnginePage):
    def __init__(
        self,
        browser_window: "BrowserWindow",
        profile: QWebEngineProfile,
        parent=None,
    ):
        super().__init__(profile, parent)
        self.browser_window = browser_window

    def createWindow(self, window_type):
        new_browser = self.browser_window.add_new_tab(
            QUrl("about:blank"),
            "Neuer Tab",
        )
        return new_browser.page()


class BrowserView(QWebEngineView):
    def __init__(
        self,
        browser_window: "BrowserWindow",
        profile: QWebEngineProfile,
    ):
        super().__init__()

        self.setPage(
            BrowserPage(browser_window, profile, self)
        )

        self.channel = QWebChannel(self.page())
        self.bridge = BrowserBridge(browser_window, self)
        self.channel.registerObject("browserBridge", self.bridge)
        self.page().setWebChannel(self.channel)


class BrowserWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()

        self.state = state
        self.project_folder = state.project_folder
        self.homepage_path = os.path.join(
            self.project_folder,
            "index.html",
        )

        self.setWindowTitle("Fizz Browser")
        self.resize(1280, 820)
        self.setMinimumSize(850, 550)

        icon_path = os.path.join(self.project_folder, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.create_interface()
        self.create_shortcuts()
        self.apply_design()

        saved_geometry = self.state.settings.value("window_geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)

        self.add_new_tab(self.homepage_url(), "Fizz Browser")

    def create_interface(self) -> None:
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbar = QToolBar()
        self.toolbar.setObjectName("browserToolbar")
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)

        self.back_button = self.make_button("←", "Zurück", self.go_back)
        self.forward_button = self.make_button("→", "Vorwärts", self.go_forward)
        self.reload_button = self.make_button("↻", "Neu laden", self.reload_current_page)
        self.home_button = self.make_button("⌂", "Startseite", self.open_homepage)

        self.address_bar = QLineEdit()
        self.address_bar.setPlaceholderText(
            "Suchen oder Webadresse eingeben"
        )
        self.address_bar.setClearButtonEnabled(True)
        self.address_bar.returnPressed.connect(
            self.navigate_from_address_bar
        )

        self.bookmark_button = self.make_button(
            "☆",
            "Lesezeichen hinzufügen",
            self.toggle_bookmark,
        )

        self.open_button = QPushButton("Öffnen")
        self.open_button.setObjectName("openButton")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(
            self.navigate_from_address_bar
        )

        self.menu_button = self.make_button(
            "⋮",
            "Menü",
            self.show_browser_menu,
        )

        for widget in (
            self.back_button,
            self.forward_button,
            self.reload_button,
            self.home_button,
            self.address_bar,
            self.bookmark_button,
            self.open_button,
            self.menu_button,
        ):
            self.toolbar.addWidget(widget)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.current_tab_changed)

        new_tab_button = QPushButton("+")
        new_tab_button.setObjectName("newTabButton")
        new_tab_button.setToolTip("Neuer Tab")
        new_tab_button.setFixedSize(42, 32)
        new_tab_button.clicked.connect(self.add_new_tab)

        self.tabs.setCornerWidget(
            new_tab_button,
            Qt.Corner.TopRightCorner,
        )

        layout.addWidget(self.toolbar)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central_widget)

    def make_button(self, text: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setFixedSize(40, 40)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def create_shortcuts(self) -> None:
        shortcuts = {
            "Ctrl+T": self.add_new_tab,
            "Ctrl+N": self.open_new_window,
            "Ctrl+W": self.close_current_tab,
            "Ctrl+L": self.focus_address_bar,
            "Ctrl+R": self.reload_current_page,
            "F5": self.reload_current_page,
            "Alt+Left": self.go_back,
            "Alt+Right": self.go_forward,
            "Alt+Home": self.open_homepage,
            "Ctrl+D": self.toggle_bookmark,
            "Ctrl+Shift+O": self.open_download_folder,
        }

        for shortcut, callback in shortcuts.items():
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)

    def homepage_url(self) -> QUrl:
        if os.path.exists(self.homepage_path):
            return QUrl.fromLocalFile(self.homepage_path)
        return QUrl("https://www.google.com")

    def is_homepage(self, url: QUrl) -> bool:
        if not url.isLocalFile():
            return False

        return os.path.normcase(os.path.abspath(url.toLocalFile())) == (
            os.path.normcase(os.path.abspath(self.homepage_path))
        )

    def add_new_tab(
        self,
        url: QUrl | None = None,
        label: str = "Neuer Tab",
    ) -> BrowserView:
        browser = BrowserView(self, self.state.profile)
        tab_index = self.tabs.addTab(browser, label)
        self.tabs.setCurrentIndex(tab_index)

        browser.urlChanged.connect(
            lambda new_url, view=browser:
            self.update_address_bar(new_url, view)
        )
        browser.titleChanged.connect(
            lambda title, view=browser:
            self.update_tab_title(view, title)
        )
        browser.iconChanged.connect(
            lambda icon, view=browser:
            self.update_tab_icon(view, icon)
        )
        browser.loadStarted.connect(
            lambda view=browser:
            self.page_load_started(view)
        )
        browser.loadFinished.connect(
            lambda success, view=browser:
            self.page_load_finished(view, success)
        )

        browser.setUrl(url or self.homepage_url())
        return browser

    def current_browser(self) -> BrowserView | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, BrowserView) else None

    def current_tab_changed(self, index: int) -> None:
        browser = self.current_browser()
        if not browser:
            return

        self.update_address_bar(browser.url(), browser)
        self.update_bookmark_button()
        title = browser.title() or "Fizz Browser"
        self.setWindowTitle(f"{title} – Fizz Browser")

    def update_address_bar(
        self,
        url: QUrl,
        browser: BrowserView,
    ) -> None:
        if browser is not self.current_browser():
            return

        if self.is_homepage(url):
            self.address_bar.clear()
        else:
            self.address_bar.setText(url.toString())
            self.address_bar.setCursorPosition(0)

        self.update_bookmark_button()

    def update_tab_title(
        self,
        browser: BrowserView,
        title: str,
    ) -> None:
        index = self.tabs.indexOf(browser)
        if index == -1:
            return

        clean_title = title.strip() if title and title.strip() else "Neuer Tab"
        if len(clean_title) > 28:
            clean_title = clean_title[:28] + "…"

        self.tabs.setTabText(index, clean_title)

        if browser is self.current_browser():
            self.setWindowTitle(
                f"{title or 'Fizz Browser'} – Fizz Browser"
            )

    def update_tab_icon(self, browser: BrowserView, icon: QIcon) -> None:
        index = self.tabs.indexOf(browser)
        if index != -1:
            self.tabs.setTabIcon(index, icon)

    def page_load_started(self, browser: BrowserView) -> None:
        if browser is self.current_browser():
            self.statusBar().showMessage("Seite wird geladen …")

    def page_load_finished(
        self,
        browser: BrowserView,
        success: bool,
    ) -> None:
        if browser is self.current_browser():
            self.statusBar().showMessage(
                "Seite geladen" if success else "Seite konnte nicht geladen werden",
                1800 if success else 3500,
            )

        if not success:
            return

        url = browser.url()
        if self.is_homepage(url):
            return

        url_text = url.toString()
        if not url_text or url_text == "about:blank":
            return

        self.state.database.add_visit(
            browser.title() or url_text,
            url_text,
        )
        self.update_bookmark_button()

    def text_to_url(self, text: str) -> QUrl:
        value = text.strip()
        if not value:
            return self.homepage_url()

        lowered = value.lower()
        if lowered.startswith((
            "http://",
            "https://",
            "file://",
            "about:",
        )):
            return QUrl(value)

        if "." in value and " " not in value:
            return QUrl("https://" + value)

        return QUrl(
            "https://www.google.com/search?q=" + quote_plus(value)
        )

    def navigate_from_address_bar(self) -> None:
        browser = self.current_browser()
        value = self.address_bar.text().strip()
        if browser and value:
            browser.setUrl(self.text_to_url(value))

    def toggle_bookmark(self) -> None:
        browser = self.current_browser()
        if not browser:
            return

        url = browser.url()
        if self.is_homepage(url) or not url.isValid():
            return

        url_text = url.toString()
        database = self.state.database

        if database.is_bookmarked(url_text):
            database.remove_bookmark(url_text)
            self.statusBar().showMessage("Lesezeichen entfernt", 1800)
        else:
            database.add_bookmark(
                browser.title() or url_text,
                url_text,
            )
            self.statusBar().showMessage("Lesezeichen gespeichert", 1800)

        self.update_bookmark_button()

    def update_bookmark_button(self) -> None:
        browser = self.current_browser()
        if not browser:
            self.bookmark_button.setText("☆")
            return

        url = browser.url()
        bookmarked = (
            url.isValid()
            and not self.is_homepage(url)
            and self.state.database.is_bookmarked(url.toString())
        )

        self.bookmark_button.setText("★" if bookmarked else "☆")
        self.bookmark_button.setToolTip(
            "Lesezeichen entfernen"
            if bookmarked
            else "Lesezeichen hinzufügen"
        )

    def show_browser_menu(self) -> None:
        menu = QMenu(self)

        actions = [
            ("Neuer Tab", self.add_new_tab, "Ctrl+T"),
            ("Neues Fenster", self.open_new_window, "Ctrl+N"),
            ("Lesezeichen umschalten", self.toggle_bookmark, "Ctrl+D"),
            ("Download-Ordner öffnen", self.open_download_folder, "Ctrl+Shift+O"),
            ("Startseite", self.open_homepage, "Alt+Home"),
        ]

        for label, callback, shortcut in actions:
            action = menu.addAction(label)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)

        menu.addSeparator()

        clear_action = menu.addAction("Verlauf löschen")
        clear_action.triggered.connect(self.confirm_clear_history)

        about_action = menu.addAction("Über Fizz Browser")
        about_action.triggered.connect(self.show_about)

        menu.exec(
            self.menu_button.mapToGlobal(
                self.menu_button.rect().bottomLeft()
            )
        )

    def confirm_clear_history(self) -> None:
        result = QMessageBox.question(
            self,
            "Verlauf löschen",
            "Möchtest du den gesamten Verlauf wirklich löschen?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if result == QMessageBox.StandardButton.Yes:
            self.state.database.clear_history()
            self.statusBar().showMessage("Verlauf gelöscht", 2000)

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "Über Fizz Browser",
            "Fizz Browser\n\nDein eigener Chromium-Browser mit "
            "Tabs, Verlauf, Lesezeichen, Downloads und mehreren Fenstern. 😊",
        )

    def open_download_folder(self) -> None:
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(self.state.download_folder)
        )

    def open_new_window(self) -> None:
        self.state.create_window()

    def open_homepage(self) -> None:
        browser = self.current_browser()
        if browser:
            browser.setUrl(self.homepage_url())

    def go_back(self) -> None:
        browser = self.current_browser()
        if browser:
            browser.back()

    def go_forward(self) -> None:
        browser = self.current_browser()
        if browser:
            browser.forward()

    def reload_current_page(self) -> None:
        browser = self.current_browser()
        if browser:
            browser.reload()

    def focus_address_bar(self) -> None:
        self.address_bar.setFocus()
        self.address_bar.selectAll()

    def close_current_tab(self) -> None:
        self.close_tab(self.tabs.currentIndex())

    def close_tab(self, index: int) -> None:
        if index < 0:
            return

        if self.tabs.count() == 1:
            browser = self.current_browser()
            if browser:
                browser.setUrl(self.homepage_url())
            return

        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if widget:
            widget.deleteLater()

    def apply_design(self) -> None:
        self.setStyleSheet("""
            QMainWindow {
                background: #101219;
            }

            QToolBar#browserToolbar {
                spacing: 7px;
                padding: 9px 12px;
                background: #20232c;
                border: none;
                border-bottom: 1px solid #303440;
            }

            QPushButton {
                min-height: 36px;
                padding: 0 13px;
                color: #e8ebf4;
                background: transparent;
                border: none;
                border-radius: 11px;
                font-size: 15px;
            }

            QPushButton:hover {
                background: #303440;
            }

            QPushButton:pressed {
                background: #3a3f4c;
            }

            QPushButton#openButton {
                color: white;
                background: #756cff;
                font-weight: 700;
                padding: 0 18px;
            }

            QPushButton#openButton:hover {
                background: #8a82ff;
            }

            QPushButton#newTabButton {
                margin: 4px 8px;
                color: white;
                background: #2a2e39;
                font-size: 20px;
            }

            QLineEdit {
                min-height: 40px;
                margin: 0 7px;
                padding: 0 16px;
                color: white;
                background: #292d37;
                border: 1px solid #393e4a;
                border-radius: 15px;
                font-size: 14px;
                selection-background-color: #756cff;
            }

            QLineEdit:focus {
                background: #30343f;
                border: 1px solid #8179ff;
            }

            QTabWidget::pane {
                border: none;
                background: #101219;
            }

            QTabBar {
                background: #181b22;
            }

            QTabBar::tab {
                min-width: 140px;
                max-width: 230px;
                height: 35px;
                margin: 5px 2px 0 2px;
                padding: 0 14px;
                color: #aeb4c2;
                background: #22252e;
                border: none;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }

            QTabBar::tab:hover {
                background: #2b303a;
            }

            QTabBar::tab:selected {
                color: white;
                background: #343945;
            }

            QMenu {
                color: #f4f6fb;
                background: #252934;
                border: 1px solid #393e4a;
                padding: 7px;
            }

            QMenu::item {
                padding: 9px 28px;
                border-radius: 7px;
            }

            QMenu::item:selected {
                background: #756cff;
            }

            QStatusBar {
                color: #aeb4c2;
                background: #20232c;
                border-top: 1px solid #303440;
            }
        """)

    def closeEvent(self, event) -> None:
        self.state.settings.setValue(
            "window_geometry",
            self.saveGeometry(),
        )
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Fizz Browser")
    app.setOrganizationName("Fizz")

    project_folder = os.path.dirname(os.path.abspath(__file__))
    state = AppState(project_folder)
    app.aboutToQuit.connect(state.close)

    state.create_window()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
