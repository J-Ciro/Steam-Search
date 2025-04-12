from pathlib import Path
import logging
import winreg as reg
from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY
from typing import Union, Optional, Dict
import os
import re
from functools import lru_cache

from .vdfs import VDF
from .loginusers import LoginUsers, LoginUser
from .library import Library
from .exceptions import SteamLibraryNotFound, SteamExecutableNotFound

STEAM_SUB_KEY = r'SOFTWARE\WOW6432Node\Valve\Steam'
UNINSTALL_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
STEAM_UNINSTALL_PREFIX = 'Steam App '

DEFAULT_STEAM_PATH = r"C:\Program Files (x86)\Steam"
STEAM_EXE = "steam.exe"
STEAM_GAMES_ICON_PATH = r"steam\games"

logger = logging.getLogger(__name__)


class Steam(object):
    def __init__(self, path: Union[str, Path] = None):
        if path is None:
            try:
                self.path = Path(self.from_registry())
            except FileNotFoundError:
                self.path = Path(DEFAULT_STEAM_PATH)
        else:
            self.path = Path(path)
            
        if not self.path.exists():
            raise FileNotFoundError(f'Could not find Steam installation at: {self.path}')
        if not self.path.joinpath(STEAM_EXE).exists():
            raise SteamExecutableNotFound(self.path)
            
        self._registry_icons_cache = None

    def from_registry(self):
        """Get Steam path from registry with multiple fallbacks"""
        try:
            with reg.OpenKey(HKEY_LOCAL_MACHINE, STEAM_SUB_KEY) as hkey:
                return reg.QueryValueEx(hkey, "InstallPath")[0]
        except FileNotFoundError:
            try:
                with reg.OpenKey(HKEY_CURRENT_USER, STEAM_SUB_KEY) as hkey:
                    return reg.QueryValueEx(hkey, "SteamPath")[0]
            except FileNotFoundError:
                raise FileNotFoundError("Could not find Steam installation in registry")

    def userdata(self, steamid: str) -> Path:
        """Returns the path to the userdata folder for the steam user."""
        return Path(self.path, 'userdata').joinpath(steamid)

    def grid_path(self, steamid: str) -> Path:
        return self.userdata(steamid).joinpath('config', 'grid')

    def config_path(self):
        """Returns the path to the Steam config folder."""
        return Path(self.path, 'config')

    def loginusers(self) -> LoginUsers:
        """Returns a dictionary of all Steam users that have logged in on this machine."""
        file_path = self.path.joinpath('config', 'loginusers.vdf')
        vdf = VDF(file_path)
        loginusers = LoginUsers()
        for user in vdf['users']:
            if vdf['users'][user].get('mostrecent'):
                vdf['users'][user]['MostRecent'] = vdf['users'][user].pop('mostrecent')
            loginusers.append(
                LoginUser(ID=user, steam_path=self.path, **vdf['users'][user])
            )
        return loginusers

    def user(self, username: str) -> LoginUser:
        """Returns a Steam user by username."""
        for user in self.loginusers():
            if user.username == username:
                return user
        raise KeyError(f'Could not find Steam user with username: {username}')

    def most_recent_user(self) -> LoginUser:
        """Returns the most recently logged in Steam user."""
        return self.loginusers().most_recent()

    def all_games(self) -> list:
        """Returns all games with their icons"""
        games = []
        for library in self.libraries():
            for game in library.games():
                game_data = {
                    'id': game.id,
                    'name': game.name,
                    'path': game.path,
                    'icon': self.get_game_icon(game.id)
                }
                games.append(game_data)
        return games

    def all_shortcuts(self) -> list:
        """Returns a list of all Steam shortcuts with icons."""
        shortcuts = []
        for user in self.loginusers():
            for shortcut in user.shortcuts():
                shortcut_data = {
                    'id': shortcut.id,
                    'name': shortcut.name,
                    'path': shortcut.path,
                    'icon': self.get_game_icon(shortcut.id)
                }
                shortcuts.append(shortcut_data)
        return shortcuts

    def game(self, name: str = None, id: int = None) -> dict:
        """Returns a Steam game by name or ID with icon information."""
        for library in self.libraries():
            for game in library.games():
                if game.name.lower() == name.lower() or game.id == id:
                    return {
                        'id': game.id,
                        'name': game.name,
                        'path': game.path,
                        'icon': self.get_game_icon(game.id)
                    }
        raise KeyError(f'Could not find Steam game with name: {name} or ID: {id}')

    def libraries(self) -> list:
        """Returns a list of all Steam libraries."""
        libraries = []
        libraries_manifest_path = Path(self.path, 'steamapps', 'libraryfolders.vdf')
        if not libraries_manifest_path.exists():
            raise SteamLibraryNotFound(libraries_manifest_path)
        try:
            library_folders = VDF(libraries_manifest_path)
        except FileNotFoundError:
            logging.warning(f'Could not find Steam libraries manifest at: {libraries_manifest_path}')
            raise
        else:
            libraries_key = 'libraryfolders' if library_folders.get('libraryfolders') else 'LibraryFolders'
            for item in library_folders[libraries_key].keys():
                if item.isdigit():
                    try:
                        path = library_folders[libraries_key][item]['path']
                    except TypeError:
                        path = library_folders[libraries_key][item]
                    libraries.append(Library(self, path))
        return libraries

    @lru_cache(maxsize=512)
    def get_game_icon(self, game_id: int) -> Optional[str]:
        """
        Get the icon path for a specific game ID.
        Search order:
        1. Registry (Uninstall entries)
        2. Local steam/games folder
        3. Local Steam cache
        """
        # Try registry first (fastest)
        registry_icon = self._get_registry_icon(game_id)
        if registry_icon:
            return registry_icon
            
        # Try steam/games folder
        games_icon = self._get_games_folder_icon(game_id)
        if games_icon:
            return games_icon
            
        # Try local cache
        return self._get_local_icon_path(game_id)

    def _load_registry_icons(self) -> Dict[int, str]:
        """Load all Steam game icons from registry"""
        if self._registry_icons_cache is not None:
            return self._registry_icons_cache
            
        icons = {}
        try:
            # Check 64-bit registry
            with reg.OpenKey(HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as key:
                self._scan_registry_icons(key, icons)
                
            # Check 32-bit registry on 64-bit systems
            try:
                with reg.OpenKey(HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, KEY_READ | KEY_WOW64_32KEY) as key:
                    self._scan_registry_icons(key, icons)
            except WindowsError:
                pass
        except WindowsError as e:
            logger.warning(f"Could not access registry: {e}")
            
        self._registry_icons_cache = icons
        return icons

    def _scan_registry_icons(self, key, icons_dict: Dict[int, str]):
        """Scan registry key for Steam icons"""
        for i in range(0, reg.QueryInfoKey(key)[0]):
            try:
                subkey_name = reg.EnumKey(key, i)
                if subkey_name.startswith(STEAM_UNINSTALL_PREFIX):
                    game_id = int(subkey_name[len(STEAM_UNINSTALL_PREFIX):])
                    with reg.OpenKey(key, subkey_name) as app_key:
                        try:
                            icon_path = reg.QueryValueEx(app_key, 'DisplayIcon')[0]
                            # Clean path (sometimes contains ",0" at the end)
                            clean_path = icon_path.split(',')[0].strip('"')
                            if os.path.exists(clean_path):
                                icons_dict[game_id] = clean_path
                        except (FileNotFoundError, WindowsError):
                            continue
            except (WindowsError, ValueError):
                continue

    def _get_registry_icon(self, game_id: int) -> Optional[str]:
        """Get icon path from registry for a specific game ID"""
        return self._load_registry_icons().get(game_id)

    def _get_games_folder_icon(self, game_id: int) -> Optional[str]:
        """Check steam/games folder for game icon"""
        games_path = self.path / STEAM_GAMES_ICON_PATH
        if not games_path.exists():
            return None
            
        # Pattern to match .ico files containing the game_id
        pattern = re.compile(rf'.*{game_id}.*\.ico', re.IGNORECASE)
        
        for file in games_path.iterdir():
            if file.is_file() and file.suffix.lower() == '.ico':
                # Check filename pattern
                if pattern.match(file.name):
                    return str(file)
                
                # Check metadata if filename doesn't match
                try:
                    with open(file, 'rb') as f:
                        if f'AppID: {game_id}'.encode() in f.read(256):
                            return str(file)
                except IOError:
                    continue
        return None

    def _get_local_icon_path(self, game_id: int) -> Optional[str]:
        """Check local Steam cache for game icon"""
        cache_locations = [
            ("appcache", "librarycache"),
            ("steam", "appcache", "librarycache"),
        ]
        
        file_formats = [
            f"{game_id}_icon.jpg",
            f"{game_id}_library_600x900.jpg",
            f"{game_id}_header.jpg",
        ]
        
        for base_path in cache_locations:
            for file_format in file_formats:
                path = self.path.joinpath(*base_path, file_format)
                if path.exists():
                    return str(path)
        return None


if __name__ == '__main__':
    steam = Steam()
    
    # Obtener todos los juegos con sus iconos
    games = steam.all_games()
    print(f"Found {len(games)} games")
    for game in games[:5]:  # Mostrar solo los primeros 5 para ejemplo
        print(f"Game: {game['name']}")
        print(f"ID: {game['id']}")
        print(f"Icon: {game['icon']}\n")
    
    # Obtener todos los accesos directos con sus iconos
    shortcuts = steam.all_shortcuts()
    print(f"Found {len(shortcuts)} shortcuts")
    for shortcut in shortcuts[:5]:  # Mostrar solo los primeros 5 para ejemplo
        print(f"Shortcut: {shortcut['name']}")
        print(f"Path: {shortcut['path']}")
        print(f"Icon: {shortcut['icon']}\n")