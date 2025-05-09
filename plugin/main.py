# -*- coding: utf-8 -*-
import webbrowser

from .steam import Steam, SteamLibraryNotFound, SteamExecutableNotFound

from flox import Flox, ICON_SETTINGS
from flox.string_matcher import string_matcher, DEFAULT_QUERY_SEARCH_PRECISION, QUERY_SEARCH_PRECISION


class SteamSearch(Flox):
    
    def query(self, query):
        try:
            self._steam = Steam(self.settings.get('steam_path', None))
            if not self.settings.get('steam_path'):
                self.settings['steam_path'] = str(self._steam.path)
            debug = self.settings.get('debug', False)
            if debug:
                self.logger_level = 'DEBUG'
            games = self._steam.all_games()
            users = self._steam.loginusers()
            most_recent_user = users.most_recent() or users[0]
            shortcuts = most_recent_user.shortcuts()
        except (SteamLibraryNotFound, SteamExecutableNotFound, FileNotFoundError):
            self.add_item(
                title="Steam library not found!",
                subtitle="Please set your Steam library path in the settings",
                method=self.open_setting_dialog,
                icon=ICON_SETTINGS
            )
            return
        for item in shortcuts + games:
            query_search_precision = QUERY_SEARCH_PRECISION[self.query_search_precision]
            match = string_matcher(query, item['name'], query_search_precision=query_search_precision or DEFAULT_QUERY_SEARCH_PRECISION)
            if match.matched or (query == "" and self.settings.get('show_on_empty_search', False)):
                icon = item.get('icon') or str(item['path'])
                score = match.score
                self.add_item(
                    title=item['name'],
                    subtitle=str(item['path']),
                    icon=str(icon),
                    method="launch_game",
                    parameters=[item['id']],
                    context=[item['id']],
                    score=int(score)
                )

    def context_menu(self, data):
        game_id = data[0]
        self.add_item(
            title="Show in Steam store",
            subtitle="Opens game's Steam store page",
            method="launch_store",
            parameters=[game_id]
        )
        self.add_item(
            title="Show News",
            subtitle="Opens game's news page in Steam",
            method="launch_news",
            parameters=[game_id]
        )
        self.add_item(
            title="Uninstall Game",
            subtitle="Uninstall this game from your Steam library",
            method="uninstall_game",
            parameters=[game_id]
        )

    def launch_game(self, game_id):
        webbrowser.open("steam://rungameid/{}".format(game_id))

    def launch_store(self, game_id):
        webbrowser.open("steam://store/{}".format(game_id))

    def uninstall_game(self, game_id):
        webbrowser.open("steam://uninstall/{}".format(game_id))

    def launch_news(self, game_id):
        webbrowser.open("steam://appnews/{}".format(game_id))


if __name__ == "__main__":
    SteamSearch()
