from pathlib import Path
import logging
import winreg as reg
from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE
from typing import Union, Optional
import requests
import tempfile
import os

from .vdfs import VDF
from .loginusers import LoginUsers, LoginUser
from .library import Library
from .exceptions import SteamLibraryNotFound, SteamExecutableNotFound

STEAM_SUB_KEY = r'SOFTWARE\WOW6432Node\Valve\Steam'

DEFAULT_STEAM_PATH = r"c:\Program Files (x86)\Steam"
STEAM_EXE = "steam.exe"

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
            raise FileNotFoundError(
                f'Could not find Steam installation at: {self.path}')
        if not self.path.joinpath(STEAM_EXE).exists():
            raise SteamExecutableNotFound(self.path)

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
        """
        Returns the path to the userdata folder for the steam user.
        """
        return Path(self.path, 'userdata').joinpath(steamid)

    def grid_path(self, steamid: str) -> Path:
        return self.userdata(steamid).joinpath('config', 'grid')

    def config_path(self):
        """
        Returns the path to the Steam config folder.
        """
        return Path(self.path, 'config')

    def loginusers(self) -> LoginUsers:
        """
        Returns a dictionary of all Steam users that have logged in on this machine.
        """
        file_path = self.path.joinpath('config', 'loginusers.vdf')
        vdf = VDF(file_path)
        loginusers = LoginUsers()
        for user in vdf['users']:
            # Sometimes Steam uses all lowercase for MostRecent. No idea why.
            if vdf['users'][user].get('mostrecent'):
                vdf['users'][user]['MostRecent'] = vdf['users'][user].pop(
                    'mostrecent')
            loginusers.append(
                LoginUser(ID=user, steam_path=self.path, **vdf['users'][user])
            )
        return loginusers

    def user(self, username: str) -> LoginUser:
        """
        Returns a Steam user by username.
        """
        for user in self.loginusers():
            if user.username == username:
                return user
        raise KeyError(f'Could not find Steam user with username: {username}')

    def most_recent_user(self) -> LoginUser:
        """
        Returns the most recently logged in Steam user.
        """
        return self.loginusers().most_recent()

    def all_games(self) -> list:
        games = []
        for library in self.libraries():
            for game in library.games():
                games.append({
                    'id': game.id,
                    'name': game.name,
                    'path': game.path,
                    'icon': self.get_game_icon(game.id)  # Add icon to game data
                })
        return games

    def all_shortcuts(self) -> list:
        """
        Returns a list of all Steam shortcuts.
        """
        shortcuts = []
        for user in self.loginusers():
            shortcuts.extend(
                user.shortcuts()
            )
        return shortcuts

    def game(self, name: str = None, id: int = None) -> dict:
        """
        Returns a Steam game by name or ID with icon information.
        """
        for library in self.libraries():
            for game in library.games():
                if game.name.lower() == name.lower() or game.id == id:
                    return {
                        'id': game.id,
                        'name': game.name,
                        'path': game.path,
                        'icon': self.get_game_icon(game.id)
                    }
        raise KeyError(
            f'Could not find Steam game with name: {name} or ID: {id}')

    def libraries(self) -> list:
        """
        Returns a list of all Steam libraries.
        """
        libraries = []
        libraries_manifest_path = Path(
            self.path, 'steamapps', 'libraryfolders.vdf')
        if not libraries_manifest_path.exists():
            raise SteamLibraryNotFound(libraries_manifest_path)
        try:
            library_folders = VDF(libraries_manifest_path)
        except FileNotFoundError:
            logging.warning(
                f'Could not find Steam libraries manifest ("libraryfolders.vdf") at: {libraries_manifest_path}')
            raise
        else:
            if library_folders.get('libraryfolders'):
                libraries_key = 'libraryfolders'
            else:
                libraries_key = 'LibraryFolders'
            for item in library_folders[libraries_key].keys():
                if item.isdigit():
                    try:
                        library_path = Library(
                            self, library_folders[libraries_key][item]['path'])
                    except TypeError:
                        library_path = Library(
                            self, library_folders[libraries_key][item])
                    libraries.append(library_path)
        return libraries

    def get_game_icon(self, game_id: int) -> Optional[str]:
        """
        Get the icon path for a specific game ID.
        
        First checks local Steam cache, then falls back to downloading from Steam CDN.
        
        Args:
            game_id: The Steam game/app ID
            
        Returns:
            Path to the icon file if found, None otherwise
        """
        # First try local cache
        icon_path = self._get_local_icon_path(game_id)
        if icon_path:
            return icon_path
            
        # If not found locally, try to download
        return self._download_icon(game_id)

    def _get_local_icon_path(self, game_id: int) -> Optional[str]:
        """
        Check local Steam cache for game icon.
        
        Args:
            game_id: The Steam game/app ID
            
        Returns:
            Path to the icon file if found, None otherwise
        """
        cache_paths = [
            os.path.join(self.path, "appcache", "librarycache", f"{game_id}_icon.jpg"),
            os.path.join(self.path, "steam", "appcache", "librarycache", f"{game_id}_icon.jpg"),
            os.path.join(self.path, "appcache", "librarycache", f"{game_id}_library_600x900.jpg"),
            os.path.join(self.path, "steam", "appcache", "librarycache", f"{game_id}_library_600x900.jpg"),
        ]
        
        for path in cache_paths:
            if os.path.exists(path):
                return path
        return None

    def _download_icon(self, game_id: int) -> Optional[str]:
        """
        Download game icon from Steam CDN.
        
        Args:
            game_id: The Steam game/app ID
            
        Returns:
            Path to the downloaded temporary icon file if successful, None otherwise
        """
        cdn_urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game_id}/library_600x900.jpg",
            f"https://media.steampowered.com/steamcommunity/public/images/apps/{game_id}/{game_id}.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{game_id}/library_600x900.jpg",
        ]
        
        for url in cdn_urls:
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    # Save to temp file
                    _, ext = os.path.splitext(url)
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                        f.write(response.content)
                        return f.name
            except requests.RequestException:
                continue
        return None


if __name__ == '__main__':
    steam = Steam()
    games = steam.all_games()
    for game in games:
        print(f"Game: {game['name']}")
        print(f"ID: {game['id']}")
        print(f"Icon: {game['icon']}\n")