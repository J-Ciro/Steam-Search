from pathlib import Path
import logging
import winreg as reg
from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY, KEY_WOW64_64KEY
from typing import Union, Optional, Dict, List, Set
import requests
import tempfile
import os
from functools import lru_cache
import pickle
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from .vdfs import VDF
from .loginusers import LoginUsers, LoginUser
from .library import Library
from .exceptions import SteamLibraryNotFound, SteamExecutableNotFound

STEAM_SUB_KEY = r'SOFTWARE\WOW6432Node\Valve\Steam'
DEFAULT_STEAM_PATH = r"C:\Program Files (x86)\Steam"
STEAM_EXE = "steam.exe"
CACHE_FILE = "steam_icon_cache.pkl"
CACHE_VERSION = 2  # Increment when cache format changes

logger = logging.getLogger(__name__)

class Steam:
    def __init__(self, path: Union[str, Path] = None):
        """
        Initialize Steam class with optional custom path.
        If no path is provided, tries to find Steam installation automatically.
        """
        self._icon_cache: Dict[int, Optional[str]] = {}
        self._library_cache: Optional[List[Library]] = None
        self._loaded_cache = False
        self._executor = ThreadPoolExecutor(max_workers=4)
        
        if path is None:
            try:
                self.path = Path(self.from_registry())
            except FileNotFoundError:
                self.path = Path(DEFAULT_STEAM_PATH)
        else:
            self.path = Path(path)
            
        if not self.path.exists():
            raise FileNotFoundError(f'Steam installation not found at: {self.path}')
        if not self.path.joinpath(STEAM_EXE).exists():
            raise SteamExecutableNotFound(self.path)
            
        self._load_icon_cache()

    def __del__(self):
        self._executor.shutdown(wait=False)
        self._save_icon_cache()

    def _load_icon_cache(self):
        """Load icon cache from disk with version checking."""
        cache_path = Path(tempfile.gettempdir()) / CACHE_FILE
        try:
            if cache_path.exists():
                with open(cache_path, 'rb') as f:
                    data = pickle.load(f)
                    if isinstance(data, dict) and data.get('version') == CACHE_VERSION:
                        self._icon_cache = data.get('cache', {})
                        logger.info(f"Loaded {len(self._icon_cache)} cached icons")
        except Exception as e:
            logger.warning(f"Failed to load icon cache: {e}")

    def _save_icon_cache(self):
        """Save icon cache to disk with versioning."""
        if not self._icon_cache:
            return
            
        cache_path = Path(tempfile.gettempdir()) / CACHE_FILE
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump({
                    'version': CACHE_VERSION,
                    'cache': self._icon_cache
                }, f)
        except Exception as e:
            logger.warning(f"Failed to save icon cache: {e}")

    def from_registry(self) -> str:
        """Get Steam path from registry with multiple fallbacks."""
        try:
            with reg.OpenKey(HKEY_LOCAL_MACHINE, STEAM_SUB_KEY) as hkey:
                return reg.QueryValueEx(hkey, "InstallPath")[0]
        except FileNotFoundError:
            with reg.OpenKey(HKEY_CURRENT_USER, STEAM_SUB_KEY) as hkey:
                return reg.QueryValueEx(hkey, "SteamPath")[0]

    # Restored methods that were accidentally removed
    def userdata(self, steamid: str) -> Path:
        """Get path to userdata folder for specific Steam user."""
        return Path(self.path, 'userdata').joinpath(steamid)

    def grid_path(self, steamid: str) -> Path:
        """Get path to grid folder containing custom images for a Steam user."""
        return self.userdata(steamid).joinpath('config', 'grid')

    def config_path(self) -> Path:
        """Get path to Steam config folder."""
        return Path(self.path, 'config')

    def loginusers(self) -> LoginUsers:
        """Get all Steam users that have logged in on this machine."""
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
        """Get Steam user by username."""
        for user in self.loginusers():
            if user.username == username:
                return user
        raise KeyError(f'Could not find Steam user with username: {username}')

    def most_recent_user(self) -> LoginUser:
        """Get the most recently logged in Steam user."""
        return self.loginusers().most_recent()

    def all_shortcuts(self) -> List[Dict]:
        """Get all Steam shortcuts from all users."""
        shortcuts = []
        for user in self.loginusers():
            shortcuts.extend(user.shortcuts())
        return shortcuts

    @lru_cache(maxsize=512)
    def get_game_icon(self, game_id: int) -> Optional[str]:
        """
        Get game icon with optimized lookup and caching.
        Priority:
        1. Memory cache
        2. Registry lookup (optimized)
        3. Local Steam files
        4. CDN download (async)
        """
        # Check memory cache first
        if game_id in self._icon_cache:
            cached = self._icon_cache[game_id]
            if cached and os.path.exists(cached):
                return cached
            if cached is None:  # Explicit None means we already checked and found nothing
                return None

        # Try registry first (fastest)
        icon_path = self._get_registry_icon_path(game_id)
        if icon_path:
            self._icon_cache[game_id] = icon_path
            return icon_path

        # Then check local Steam files
        icon_path = self._get_local_icon_path(game_id)
        if icon_path:
            self._icon_cache[game_id] = icon_path
            return icon_path

        # Finally try CDN (async)
        future = self._executor.submit(self._download_icon, game_id)
        icon_path = future.result()  # Blocking but runs in parallel
        self._icon_cache[game_id] = icon_path
        return icon_path

    def _get_registry_icon_path(self, game_id: int) -> Optional[str]:
        """Optimized registry lookup with multiple key attempts."""
        app_key = f"Steam App {game_id}"
        registry_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        ]
        
        # Try different registry views
        for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY]:
            for base_path in registry_paths:
                try:
                    with reg.OpenKey(HKEY_LOCAL_MACHINE, f"{base_path}\\{app_key}", 
                                   access=KEY_READ | view) as key:
                        
                        # Check common value names
                        for value_name in ["DisplayIcon", "IconPath", "InstallIcon"]:
                            try:
                                icon_path, _ = reg.QueryValueEx(key, value_name)
                                if "," in icon_path:
                                    icon_path = icon_path.split(",")[0]
                                icon_path = os.path.expandvars(icon_path)
                                if os.path.exists(icon_path):
                                    return os.path.abspath(icon_path)
                            except (FileNotFoundError, OSError):
                                continue

                        # Try InstallLocation fallback
                        try:
                            install_path, _ = reg.QueryValueEx(key, "InstallLocation")
                            if install_path:
                                install_path = os.path.expandvars(install_path)
                                for ext in ['.ico', '.png', '.jpg']:
                                    for name in ['icon', 'game', f'{game_id}', 'steam_icon']:
                                        path = os.path.join(install_path, name + ext)
                                        if os.path.exists(path):
                                            return os.path.abspath(path)
                        except (FileNotFoundError, OSError):
                            continue
                except (FileNotFoundError, OSError):
                    continue
        return None

    def _get_local_icon_path(self, game_id: int) -> Optional[str]:
        """Optimized local file lookup with path caching."""
        cache_dirs = [
            ("appcache", "librarycache"),
            ("steam", "appcache", "librarycache")
        ]
        
        for rel_path in cache_dirs:
            cache_dir = self.path.joinpath(*rel_path)
            if not cache_dir.exists():
                continue
                
            for pattern in [f"{game_id}_icon.jpg", f"{game_id}_library_600x900.jpg", f"{game_id}.jpg"]:
                icon_path = cache_dir / pattern
                if icon_path.exists():
                    return str(icon_path)
        return None

    def _download_icon(self, game_id: int) -> Optional[str]:
        """Optimized CDN download with caching."""
        cdn_urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game_id}/library_600x900.jpg",
            f"https://media.steampowered.com/steamcommunity/public/images/apps/{game_id}/{game_id}.jpg"
        ]
        
        for url in cdn_urls:
            try:
                response = requests.get(url, timeout=3, stream=True)
                if response.status_code == 200:
                    cache_dir = Path(tempfile.gettempdir()) / "steam_icons"
                    cache_dir.mkdir(exist_ok=True)
                    
                    # Create consistent filename based on URL hash
                    url_hash = hashlib.md5(url.encode()).hexdigest()
                    ext = os.path.splitext(url)[1] or '.jpg'
                    cache_file = cache_dir / f"{game_id}_{url_hash}{ext}"
                    
                    if not cache_file.exists():
                        with open(cache_file, 'wb') as f:
                            for chunk in response.iter_content(1024):
                                f.write(chunk)
                    
                    return str(cache_file)
            except requests.RequestException:
                continue
        return None

    def all_games(self) -> List[Dict]:
        """Get all games with parallel icon prefetching."""
        games = []
        game_ids = set()
        
        # First collect all games
        for library in self.libraries():
            for game in library.games():
                games.append({
                    'id': game.id,
                    'name': game.name,
                    'path': game.path,
                    'icon': None  # Will be filled later
                })
                game_ids.add(int(game.id))
        
        # Prefetch icons in parallel
        futures = {
            self._executor.submit(self.get_game_icon, game_id): game_id
            for game_id in game_ids
        }
        
        # Map game IDs to their icon paths
        icon_map = {}
        for future in as_completed(futures):
            game_id = futures[future]
            try:
                icon_map[game_id] = future.result()
            except Exception as e:
                logger.warning(f"Failed to get icon for game {game_id}: {e}")
                icon_map[game_id] = None
        
        # Update games with icons
        for game in games:
            game['icon'] = icon_map.get(int(game['id']), game['path'])
        
        return games

    def libraries(self) -> List[Library]:
        """Get libraries with caching."""
        if self._library_cache is not None:
            return self._library_cache
            
        libraries = []
        manifest_path = self.path.joinpath('steamapps', 'libraryfolders.vdf')
        
        if not manifest_path.exists():
            raise SteamLibraryNotFound(manifest_path)
            
        try:
            library_folders = VDF(manifest_path)
            libraries_key = 'libraryfolders' if 'libraryfolders' in library_folders else 'LibraryFolders'
            
            for key, value in library_folders[libraries_key].items():
                if key.isdigit():
                    path = value.get('path', value) if isinstance(value, dict) else value
                    libraries.append(Library(self, path))
                    
            self._library_cache = libraries
            return libraries
        except Exception as e:
            logger.error(f"Failed to load libraries: {e}")
            raise

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
                if (name and game.name.lower() == name.lower()) or (id and game.id == id):
                    return {
                        'id': game.id,
                        'name': game.name,
                        'path': game.path,
                        'icon': self.get_game_icon(game.id)
                    }
        raise KeyError(f'Could not find Steam game with name: {name} or ID: {id}')


if __name__ == '__main__':
    # Example usage
    steam = Steam()
    games = steam.all_games()
    
    for game in games:
        print(f"Game: {game['name']}")
        print(f"ID: {game['id']}")
        print(f"Path: {game['path']}")
        print(f"Icon: {game['icon']}\n")