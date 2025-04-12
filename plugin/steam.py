from pathlib import Path
import logging
import winreg as reg
from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE
from typing import Union, Optional, Dict, List
import requests
import tempfile
import os
from functools import lru_cache

from .vdfs import VDF
from .loginusers import LoginUsers, LoginUser
from .library import Library
from .exceptions import SteamLibraryNotFound, SteamExecutableNotFound

STEAM_SUB_KEY = r'SOFTWARE\WOW6432Node\Valve\Steam'
DEFAULT_STEAM_PATH = r"c:\Program Files (x86)\Steam"
STEAM_EXE = "steam.exe"

logger = logging.getLogger(__name__)

# Caché global para iconos por ID
_icon_cache: Dict[int, Optional[str]] = {}

class Steam:
    def __init__(self, path: Union[str, Path] = None):
        """
        Initialize Steam class with optional custom path.
        If no path is provided, tries to find Steam installation automatically.
        
        Args:
            path: Optional custom path to Steam installation
        """
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

    def from_registry(self) -> str:
        """
        Get Steam installation path from Windows registry with multiple fallbacks.
        
        Returns:
            str: Path to Steam installation
            
        Raises:
            FileNotFoundError: If Steam path cannot be found in registry
        """
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
        Get path to userdata folder for specific Steam user.
        
        Args:
            steamid: SteamID of the user
            
        Returns:
            Path: Path to userdata folder
        """
        return Path(self.path, 'userdata').joinpath(steamid)

    def grid_path(self, steamid: str) -> Path:
        """
        Get path to grid folder containing custom images for a Steam user.
        
        Args:
            steamid: SteamID of the user
            
        Returns:
            Path: Path to grid folder
        """
        return self.userdata(steamid).joinpath('config', 'grid')

    def config_path(self) -> Path:
        """
        Get path to Steam config folder.
        
        Returns:
            Path: Path to config folder
        """
        return Path(self.path, 'config')

    def loginusers(self) -> LoginUsers:
        """
        Get all Steam users that have logged in on this machine.
        
        Returns:
            LoginUsers: Collection of LoginUser objects
        """
        file_path = self.path.joinpath('config', 'loginusers.vdf')
        vdf = VDF(file_path)
        loginusers = LoginUsers()
        
        for user in vdf['users']:
            # Handle case where 'MostRecent' is lowercase
            if vdf['users'][user].get('mostrecent'):
                vdf['users'][user]['MostRecent'] = vdf['users'][user].pop('mostrecent')
                
            loginusers.append(
                LoginUser(ID=user, steam_path=self.path, **vdf['users'][user])
            )
        return loginusers

    def user(self, username: str) -> LoginUser:
        """
        Get Steam user by username.
        
        Args:
            username: Username to search for
            
        Returns:
            LoginUser: User object
            
        Raises:
            KeyError: If user not found
        """
        for user in self.loginusers():
            if user.username == username:
                return user
        raise KeyError(f'Could not find Steam user with username: {username}')

    def most_recent_user(self) -> LoginUser:
        """
        Get the most recently logged in Steam user.
        
        Returns:
            LoginUser: Most recent user
        """
        return self.loginusers().most_recent()

    @lru_cache(maxsize=512)
    def get_game_icon(self, game_id: int) -> Optional[str]:
        """
        Get game icon with multiple fallback sources and caching.
        
        Priority:
        1. Local cache
        2. Steam local files
        3. Windows registry
        4. Steam CDN download
        
        Args:
            game_id: Steam game/app ID
            
        Returns:
            Optional[str]: Path to icon file if found, None otherwise
        """
        # Check global cache first
        if game_id in _icon_cache:
            return _icon_cache[game_id]
        
        # 1. Check local Steam paths
        icon_path = self._get_local_icon_path(game_id)
        if icon_path:
            _icon_cache[game_id] = icon_path
            return icon_path
            
        # 2. Check Windows registry
        icon_path = self._get_registry_icon_path(game_id)
        if icon_path:
            _icon_cache[game_id] = icon_path
            return icon_path
            
        # 3. Try downloading from Steam CDN
        icon_path = self._download_icon(game_id)
        _icon_cache[game_id] = icon_path  # Can be None if download fails
        return icon_path

    def _get_local_icon_path(self, game_id: int) -> Optional[str]:
        """
        Check local Steam cache for game icon.
        
        Args:
            game_id: Steam game/app ID
            
        Returns:
            Optional[str]: Path to icon if found, None otherwise
        """
        cache_paths = [
            os.path.join(self.path, "appcache", "librarycache", f"{game_id}_icon.jpg"),
            os.path.join(self.path, "steam", "appcache", "librarycache", f"{game_id}_icon.jpg"),
            os.path.join(self.path, "appcache", "librarycache", f"{game_id}_library_600x900.jpg"),
            os.path.join(self.path, "steam", "appcache", "librarycache", f"{game_id}_library_600x900.jpg"),
            os.path.join(self.path, "appcache", "librarycache", f"{game_id}.jpg"),
        ]
        
        for path in cache_paths:
            if os.path.exists(path):
                return path
        return None

    def _get_registry_icon_path(self, game_id: int) -> Optional[str]:
        """
        Search Windows registry for game icon path.
        
        Args:
            game_id: Steam game/app ID
            
        Returns:
            Optional[str]: Path to icon if found in registry, None otherwise
        """
        try:
            app_key = f"Steam App {game_id}"
            registry_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
            
            with reg.OpenKey(reg.HKEY_LOCAL_MACHINE, f"{registry_path}\\{app_key}") as key:
                # Try common registry values containing icon paths
                for value_name in ["DisplayIcon", "IconPath", "InstallIcon"]:
                    try:
                        icon_path, _ = reg.QueryValueEx(key, value_name)
                        
                        # Handle cases like "path,0"
                        if "," in icon_path:
                            icon_path = icon_path.split(",")[0]
                            
                        if os.path.exists(icon_path):
                            return icon_path
                    except FileNotFoundError:
                        continue
                    except:
                        pass

                # Try to build path from InstallLocation
                try:
                    install_path, _ = reg.QueryValueEx(key, "InstallLocation")
                    potential_paths = [
                        os.path.join(install_path, "icon.ico"),
                        os.path.join(install_path, "icon.png"),
                        os.path.join(install_path, "game.ico"),
                        os.path.join(install_path, f"{game_id}.ico"),
                        os.path.join(install_path, "steam_icon.ico")
                    ]
                    
                    for path in potential_paths:
                        if os.path.exists(path):
                            return path
                except:
                    pass
        except:
            pass
            
        return None

    def _download_icon(self, game_id: int) -> Optional[str]:
        """
        Download game icon from Steam CDN.
        
        Args:
            game_id: Steam game/app ID
            
        Returns:
            Optional[str]: Path to temporary downloaded icon file if successful, None otherwise
        """
        cdn_urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game_id}/library_600x900.jpg",
            f"https://media.steampowered.com/steamcommunity/public/images/apps/{game_id}/{game_id}.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{game_id}/library_600x900.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game_id}/capsule_184x69.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game_id}/header.jpg",
        ]
        
        for url in cdn_urls:
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    _, ext = os.path.splitext(url)
                    with tempfile.NamedTemporaryFile(suffix=ext or '.jpg', delete=False) as f:
                        f.write(response.content)
                        return f.name
            except requests.RequestException:
                continue
        return None

    def all_games(self) -> List[Dict]:
        """
        Get all games from all Steam libraries with icon information.
        
        Returns:
            List[Dict]: List of game dictionaries with id, name, path and icon
        """
        games = []
        for library in self.libraries():
            for game in library.games():
                games.append({
                    'id': game.id,
                    'name': game.name,
                    'path': game.path,
                    'icon': self.get_game_icon(int(game.id)) or str(game.path)  # Fallback to game path if no icon
                })
        return games

    def all_shortcuts(self) -> List[Dict]:
        """
        Get all Steam shortcuts from all users.
        
        Returns:
            List[Dict]: List of shortcut dictionaries
        """
        shortcuts = []
        for user in self.loginusers():
            shortcuts.extend(user.shortcuts())
        return shortcuts

    def game(self, name: str = None, id: int = None) -> Dict:
        """
        Get specific game by name or ID with icon information.
        
        Args:
            name: Game name (optional)
            id: Game ID (optional)
            
        Returns:
            Dict: Game dictionary with id, name, path and icon
            
        Raises:
            KeyError: If game not found
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
        raise KeyError(f'Could not find Steam game with name: {name} or ID: {id}')

    def libraries(self) -> List[Library]:
        """
        Get all Steam libraries.
        
        Returns:
            List[Library]: List of Library objects
            
        Raises:
            SteamLibraryNotFound: If libraryfolders.vdf not found
        """
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
            # Handle different key names in libraryfolders.vdf
            libraries_key = 'libraryfolders' if library_folders.get('libraryfolders') else 'LibraryFolders'
            
            for item in library_folders[libraries_key].keys():
                if item.isdigit():
                    try:
                        library_path = Library(
                            self, 
                            library_folders[libraries_key][item]['path']
                        )
                    except TypeError:
                        library_path = Library(
                            self, 
                            library_folders[libraries_key][item]
                        )
                    libraries.append(library_path)
        return libraries


if __name__ == '__main__':
    # Example usage
    steam = Steam()
    games = steam.all_games()
    
    for game in games:
        print(f"Game: {game['name']}")
        print(f"ID: {game['id']}")
        print(f"Path: {game['path']}")
        print(f"Icon: {game['icon']}\n")