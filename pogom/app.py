#!/usr/bin/python
# -*- coding: utf-8 -*-

import calendar
import logging

from flask import Flask, abort, jsonify, render_template, request
from flask.json import JSONEncoder
from flask_compress import Compress
from datetime import datetime
from s2sphere import LatLng
from pogom.scout import perform_scout
from pogom.utils import get_args
from datetime import timedelta
from collections import OrderedDict
from bisect import bisect_left

from . import config
from .models import Pokemon, Gym, Pokestop, ScannedLocation, MainWorker, WorkerStatus
from .utils import now, dottedQuadToNum, get_blacklist
log = logging.getLogger(__name__)
compress = Compress()


class Pogom(Flask):
    def __init__(self, import_name, **kwargs):
        super(Pogom, self).__init__(import_name, **kwargs)
        compress.init_app(self)

        args = get_args()

        # Global blist
        if not args.disable_blacklist:
            log.info('Retrieving blacklist...')
            self.blacklist = get_blacklist()
            # Sort & index for binary search
            self.blacklist.sort(key=lambda r: r[0])
            self.blacklist_keys = [
                dottedQuadToNum(r[0]) for r in self.blacklist
            ]
        else:
            log.info('Blacklist disabled for this session.')
            self.blacklist = []
            self.blacklist_keys = []

        # Routes
        self.json_encoder = CustomJSONEncoder
        self.route("/", methods=['GET'])(self.fullmap)
        self.route("/raw_data", methods=['GET'])(self.raw_data)
        self.route("/spawn_history", methods=['GET'])(self.spawn_history)
        self.route("/loc", methods=['GET'])(self.loc)
        self.route("/next_loc", methods=['POST'])(self.next_loc)
        self.route("/mobile", methods=['GET'])(self.list_pokemon)
        self.route("/search_control", methods=['GET'])(self.get_search_control)
        self.route("/search_control", methods=['POST'])(self.post_search_control)
        self.route("/stats", methods=['GET'])(self.get_stats)
        self.route("/status", methods=['GET'])(self.get_status)
        self.route("/status", methods=['POST'])(self.post_status)
        self.route("/spawn_data", methods=['GET'])(self.get_spawndata)
        self.route("/gym_data", methods=['GET'])(self.get_gymdata)
        self.route("/spawn_history2", methods=['GET'])(self.spawn_history)
        self.route("/robots.txt", methods=['GET'])(self.render_robots_txt)
        self.route("/scout", methods=['GET'])(self.get_scout_data)


    def get_scout_data(self):
        encounterId = request.args.get('encounter_id')
        p = Pokemon.get(Pokemon.encounter_id == encounterId)
        return jsonify(perform_scout(p))

    def render_robots_txt(self):
        return render_template('robots.txt')

    def spawn_history2(self):
        d = {}
        spawnpoint_id = request.args.get('spawnpoint_id')
        d['spawn_history2'] = Pokemon.get_spawn_history(spawnpoint_id)

        return jsonify(d)

    def validate_request(self):
        args = get_args()
        ip_addr = request.remote_addr
        if ip_addr in args.trusted_proxies:
            ip_addr = request.headers.get('X-Forwarded-For', ip_addr)
        if self._ip_is_blacklisted(ip_addr):
            log.debug('Denied access to %s.', ip_addr)
            abort(403)

    def _ip_is_blacklisted(self, ip):
        if not self.blacklist:
            return False

        # Get the nearest IP range
        pos = max(bisect_left(self.blacklist_keys, ip) - 1, 0)
        ip_range = self.blacklist[pos]

        start = dottedQuadToNum(ip_range[0])
        end = dottedQuadToNum(ip_range[1])

        return start <= dottedQuadToNum(ip) <= end

    def set_search_control(self, control):
        self.search_control = control

    def set_heartbeat_control(self, heartb):
        self.heartbeat = heartb

    def set_location_queue(self, queue):
        self.location_queue = queue

    def set_current_location(self, location):
        self.current_location = location

    def get_search_control(self):
        return jsonify({'status': not self.search_control.is_set()})

    def post_search_control(self):
        args = get_args()
        if not args.search_control or args.on_demand_timeout > 0:
            return 'Search control is disabled', 403
        action = request.args.get('action', 'none')
        if action == 'on':
            self.search_control.clear()
            log.info('Search thread resumed')
        elif action == 'off':
            self.search_control.set()
            log.info('Search thread paused')
        else:
            return jsonify({'message': 'invalid use of api'})
        return self.get_search_control()

    def fullmap(self):
        self.heartbeat[0] = now()
        args = get_args()
        if args.on_demand_timeout > 0:
            self.search_control.clear()
        fixed_display = "none" if args.fixed_location else "inline"
        search_display = "inline" if args.search_control and args.on_demand_timeout <= 0 else "none"
        scan_display = "none" if (args.only_server or args.fixed_location or args.spawnpoint_scanning) else "inline"

        return render_template('map.html',
                               lat=self.current_location[0],
                               lng=self.current_location[1],
                               gmaps_key=config['GMAPS_KEY'],
                               lang=config['LOCALE'],
                               is_fixed=fixed_display,
                               search_control=search_display,
                               show_scan=scan_display
                               )
    def spawn_history(self):
        d = {}
        spawnpoint_id = request.args.get('spawnpoint_id')
        d['spawn_history'] = Pokemon.get_spawn_history(spawnpoint_id)

        return jsonify(d)

    def raw_data(self):
        self.heartbeat[0] = now()
        args = get_args()

        if not request.args:
            log.info('No arguments - possible scraper')
            return ("STAYOUTAMYMAP")

        if 'curl' in request.headers.get('User-Agent'):
            log.info('Curl request - possible scraper')
            return("STAYOUTAMYMAP")

        if args.on_demand_timeout > 0:
            self.search_control.clear()
        d = {}

        # Request time of this request
        d['timestamp'] = datetime.utcnow()

        # Request time of previous request
        if request.args.get('timestamp'):
            timestamp = int(request.args.get('timestamp'))
            timestamp -= 1000  # Overlap, for rounding errors.
        else:
            timestamp = 0

        swLat = request.args.get('swLat')
        swLng = request.args.get('swLng')
        neLat = request.args.get('neLat')
        neLng = request.args.get('neLng')

        oSwLat = request.args.get('oSwLat')
        oSwLng = request.args.get('oSwLng')
        oNeLat = request.args.get('oNeLat')
        oNeLng = request.args.get('oNeLng')

        # Previous switch settings
        lastgyms = request.args.get('lastgyms')
        lastpokestops = request.args.get('lastpokestops')
        lastpokemon = request.args.get('lastpokemon')
        lastslocs = request.args.get('lastslocs')
        lastspawns = request.args.get('lastspawns')

        if request.args.get('luredonly', 'true') == 'true':
            luredonly = True
        else:
            luredonly = False

        # Current switch settings saved for next request
        if request.args.get('gyms', 'true') == 'true':
            d['lastgyms'] = request.args.get('gyms', 'true')

        if request.args.get('pokestops', 'true') == 'true':
            d['lastpokestops'] = request.args.get('pokestops', 'true')

        if request.args.get('pokemon', 'true') == 'true':
            d['lastpokemon'] = request.args.get('pokemon', 'true')

        if request.args.get('scanned', 'true') == 'true':
            d['lastslocs'] = request.args.get('scanned', 'true')

        if request.args.get('spawnpoints', 'false') == 'true':
            d['lastspawns'] = request.args.get('spawnpoints', 'false')

        # If old coords are not equal to current coords we have moved/zoomed!
        if oSwLng < swLng and oSwLat < swLat and oNeLat > neLat and oNeLng > neLng:
            newArea = False  # We zoomed in no new area uncovered
        elif not (oSwLat == swLat and oSwLng == swLng and oNeLat == neLat and oNeLng == neLng):
            newArea = True
        else:
            newArea = False

        # Pass current coords as old coords.
        d['oSwLat'] = swLat
        d['oSwLng'] = swLng
        d['oNeLat'] = neLat
        d['oNeLng'] = neLng

        if request.args.get('pokemon', 'true') == 'true':
            if request.args.get('ids'):
                ids = [int(x) for x in request.args.get('ids').split(',')]
                d['pokemons'] = Pokemon.get_active_by_id(ids, swLat, swLng,
                                                         neLat, neLng)
            elif lastpokemon != 'true':
                # If this is first request since switch on, load all pokemon on screen.
                d['pokemons'] = Pokemon.get_active(swLat, swLng, neLat, neLng)
            else:
                # If map is already populated only request modified Pokemon since last request time
                d['pokemons'] = Pokemon.get_active(swLat, swLng, neLat, neLng, timestamp=timestamp)
                if newArea:
                    # If screen is moved add newly uncovered Pokemon to the ones that were modified since last request time
                    d['pokemons'] = d['pokemons'] + (Pokemon.get_active(swLat, swLng, neLat, neLng, oSwLat=oSwLat, oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng))

            if request.args.get('eids'):
                # Exclude id's of pokemon that are hidden
                eids = [int(x) for x in request.args.get('eids').split(',')]
                d['pokemons'] = [x for x in d['pokemons'] if x['pokemon_id'] not in eids]

            if request.args.get('reids'):
                reids = [int(x) for x in request.args.get('reids').split(',')]
                d['pokemons'] = d['pokemons'] + (Pokemon.get_active_by_id(reids, swLat, swLng, neLat, neLng))
                d['reids'] = reids

        if request.args.get('pokestops', 'true') == 'true':
            if lastpokestops != 'true':
                d['pokestops'] = Pokestop.get_stops(swLat, swLng, neLat, neLng, lured=luredonly)
            else:
                d['pokestops'] = Pokestop.get_stops(swLat, swLng, neLat, neLng, timestamp=timestamp)
                if newArea:
                    d['pokestops'] = d['pokestops'] + (Pokestop.get_stops(swLat, swLng, neLat, neLng, oSwLat=oSwLat, oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng, lured=luredonly))

        if request.args.get('gyms', 'true') == 'true':
            if lastgyms != 'true':
                d['gyms'] = Gym.get_gyms(swLat, swLng, neLat, neLng)
            else:
                d['gyms'] = Gym.get_gyms(swLat, swLng, neLat, neLng, timestamp=timestamp)
                if newArea:
                    d['gyms'].update(Gym.get_gyms(swLat, swLng, neLat, neLng, oSwLat=oSwLat, oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng))

        if request.args.get('scanned', 'true') == 'true':
            if lastslocs != 'true':
                d['scanned'] = ScannedLocation.get_recent(swLat, swLng, neLat, neLng)
            else:
                d['scanned'] = ScannedLocation.get_recent(swLat, swLng, neLat, neLng, timestamp=timestamp)
                if newArea:
                    d['scanned'] = d['scanned'] + (ScannedLocation.get_recent(swLat, swLng, neLat, neLng, oSwLat=oSwLat, oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng))

        selected_duration = None

        # for stats and changed nest points etc, limit pokemon queried
        for duration in self.get_valid_stat_input()["duration"]["items"].values():
            if duration["selected"] == "SELECTED":
                selected_duration = duration["value"]
                break

        if request.args.get('seen', 'false') == 'true':
            d['seen'] = Pokemon.get_seen(selected_duration)

        if request.args.get('appearances', 'false') == 'true':
            d['appearances'] = Pokemon.get_appearances(request.args.get('pokemonid'), selected_duration)

        if request.args.get('appearancesDetails', 'false') == 'true':
            d['appearancesTimes'] = Pokemon.get_appearances_times_by_spawnpoint(request.args.get('pokemonid'),
                                                                                request.args.get('spawnpoint_id'),
                                                                                selected_duration)

        if request.args.get('spawnpoints', 'false') == 'true':
            if lastspawns != 'true':
                d['spawnpoints'] = Pokemon.get_spawnpoints(swLat=swLat, swLng=swLng, neLat=neLat, neLng=neLng)
            else:
                d['spawnpoints'] = Pokemon.get_spawnpoints(swLat=swLat, swLng=swLng, neLat=neLat, neLng=neLng, timestamp=timestamp)
                if newArea:
                    d['spawnpoints'] = d['spawnpoints'] + (Pokemon.get_spawnpoints(swLat, swLng, neLat, neLng, oSwLat=oSwLat, oSwLng=oSwLng, oNeLat=oNeLat, oNeLng=oNeLng))

        if request.args.get('status', 'false') == 'true':
            args = get_args()
            d = {}
            if args.status_page_password is None:
                d['error'] = 'Access denied'
            elif request.args.get('password', None) == args.status_page_password:
                d['main_workers'] = MainWorker.get_all()
                d['workers'] = WorkerStatus.get_all()
        return jsonify(d)

    def loc(self):
        d = {}
        d['lat'] = self.current_location[0]
        d['lng'] = self.current_location[1]

        return jsonify(d)

    def next_loc(self):
        args = get_args()
        if args.fixed_location:
            return 'Location changes are turned off', 403
        # part of query string
        if request.args:
            lat = request.args.get('lat', type=float)
            lon = request.args.get('lon', type=float)
        # from post requests
        if request.form:
            lat = request.form.get('lat', type=float)
            lon = request.form.get('lon', type=float)

        if not (lat and lon):
            log.warning('Invalid next location: %s,%s', lat, lon)
            return 'bad parameters', 400
        else:
            self.location_queue.put((lat, lon, 0))
            self.set_current_location((lat, lon, 0))
            log.info('Changing next location: %s,%s', lat, lon)
            return self.loc()

    def list_pokemon(self):
        # todo: check if client is android/iOS/Desktop for geolink, currently
        # only supports android
        pokemon_list = []

        # Allow client to specify location
        lat = request.args.get('lat', self.current_location[0], type=float)
        lon = request.args.get('lon', self.current_location[1], type=float)
        origin_point = LatLng.from_degrees(lat, lon)

        for pokemon in Pokemon.get_active(None, None, None, None):
            pokemon_point = LatLng.from_degrees(pokemon['latitude'],
                                                pokemon['longitude'])
            diff = pokemon_point - origin_point
            diff_lat = diff.lat().degrees
            diff_lng = diff.lng().degrees
            direction = (('N' if diff_lat >= 0 else 'S')
                         if abs(diff_lat) > 1e-4 else '') +\
                        (('E' if diff_lng >= 0 else 'W')
                         if abs(diff_lng) > 1e-4 else '')
            entry = {
                'id': pokemon['pokemon_id'],
                'name': pokemon['pokemon_name'],
                'card_dir': direction,
                'distance': int(origin_point.get_distance(
                    pokemon_point).radians * 6366468.241830914),
                'time_to_disappear': '%d min %d sec' % (divmod((
                    pokemon['disappear_time'] - datetime.utcnow()).seconds, 60)),
                'disappear_time': pokemon['disappear_time'],
                'disappear_sec': (pokemon['disappear_time'] - datetime.utcnow()).seconds,
                'latitude': pokemon['latitude'],
                'longitude': pokemon['longitude']
            }
            pokemon_list.append((entry, entry['distance']))
        pokemon_list = [y[0] for y in sorted(pokemon_list, key=lambda x: x[1])]
        return render_template('mobile_list.html',
                               pokemon_list=pokemon_list,
                               origin_lat=lat,
                               origin_lng=lon)

    def get_valid_stat_input(self):
        duration = request.args.get("duration", type=str)
        sort = request.args.get("sort", type=str)
        order = request.args.get("order", type=str)
        valid_durations = OrderedDict()
        valid_durations["1h"] = {"display": "Last Hour", "value": timedelta(hours=1), "selected": ("SELECTED" if duration == "1h" else "")}
        valid_durations["2h"] = {"display": "Last 2 Hours", "value": timedelta(hours=2), "selected": ("SELECTED" if duration == "2h" else "")}
        valid_durations["3h"] = {"display": "Last 3 Hours", "value": timedelta(hours=3), "selected": ("SELECTED" if duration == "3h" else "")}
        valid_durations["4h"] = {"display": "Last 4 Hours", "value": timedelta(hours=4), "selected": ("SELECTED" if duration == "4h" else "")}
        valid_durations["5h"] = {"display": "Last 5 Hours", "value": timedelta(hours=5), "selected": ("SELECTED" if duration == "5h" else "")}
        valid_durations["6h"] = {"display": "Last 6 Hours", "value": timedelta(hours=6), "selected": ("SELECTED" if duration == "6h" else "")}
        valid_durations["7h"] = {"display": "Last 7 Hours", "value": timedelta(hours=7), "selected": ("SELECTED" if duration == "7h" else "")}
        valid_durations["8h"] = {"display": "Last 8 Hours", "value": timedelta(hours=8), "selected": ("SELECTED" if duration == "8h" else "")}
        valid_durations["9h"] = {"display": "Last 9 Hours", "value": timedelta(hours=9), "selected": ("SELECTED" if duration == "9h" else "")}
        valid_durations["10h"] = {"display": "Last 10 Hours", "value": timedelta(hours=10), "selected": ("SELECTED" if duration == "10h" else "")}
        valid_durations["11h"] = {"display": "Last 11 Hours", "value": timedelta(hours=11), "selected": ("SELECTED" if duration == "11h" else "")}
        valid_durations["12h"] = {"display": "Last 12 Hours", "value": timedelta(hours=12), "selected": ("SELECTED" if duration == "12h" else "")}
        valid_durations["13h"] = {"display": "Last 13 Hours", "value": timedelta(hours=13), "selected": ("SELECTED" if duration == "13h" else "")}
        valid_durations["14h"] = {"display": "Last 14 Hours", "value": timedelta(hours=14), "selected": ("SELECTED" if duration == "14h" else "")}
        valid_durations["15h"] = {"display": "Last 15 Hours", "value": timedelta(hours=15), "selected": ("SELECTED" if duration == "15h" else "")}
        valid_durations["16h"] = {"display": "Last 16 Hours", "value": timedelta(hours=16), "selected": ("SELECTED" if duration == "16h" else "")}
        valid_durations["17h"] = {"display": "Last 17 Hours", "value": timedelta(hours=17), "selected": ("SELECTED" if duration == "17h" else "")}
        valid_durations["18h"] = {"display": "Last 18 Hours", "value": timedelta(hours=18), "selected": ("SELECTED" if duration == "18h" else "")}
        valid_durations["19h"] = {"display": "Last 19 Hours", "value": timedelta(hours=19), "selected": ("SELECTED" if duration == "19h" else "")}
        valid_durations["20h"] = {"display": "Last 20 Hours", "value": timedelta(hours=20), "selected": ("SELECTED" if duration == "20h" else "")}
        valid_durations["21h"] = {"display": "Last 21 Hours", "value": timedelta(hours=21), "selected": ("SELECTED" if duration == "21h" else "")}
        valid_durations["22h"] = {"display": "Last 22 Hours", "value": timedelta(hours=22), "selected": ("SELECTED" if duration == "22h" else "")}
        valid_durations["23h"] = {"display": "Last 23 Hours", "value": timedelta(hours=23), "selected": ("SELECTED" if duration == "23h" else "")}
        valid_durations["1d"] = {"display": "Last Day", "value": timedelta(days=1), "selected": ("SELECTED" if duration == "1d" else "")}
        valid_durations["2d"] = {"display": "Last 2 Days", "value": timedelta(days=2), "selected": ("SELECTED" if duration == "2d" else "")}
        valid_durations["3d"] = {"display": "Last 3 Days", "value": timedelta(days=3), "selected": ("SELECTED" if duration == "3d" else "")}
        valid_durations["4d"] = {"display": "Last 4 Days", "value": timedelta(days=4), "selected": ("SELECTED" if duration == "4d" else "")}
        valid_durations["5d"] = {"display": "Last 5 Days", "value": timedelta(days=5), "selected": ("SELECTED" if duration == "5d" else "")}
        valid_durations["6d"] = {"display": "Last 6 Days", "value": timedelta(days=6), "selected": ("SELECTED" if duration == "6d" else "")}
        valid_durations["7d"] = {"display": "Last 7 Days", "value": timedelta(days=7), "selected": ("SELECTED" if duration == "7d" else "")}
        valid_durations["10d"] = {"display": "Last 10 Days", "value": timedelta(days=10), "selected": ("SELECTED" if duration == "10d" else "")}
        valid_durations["14d"] = {"display": "Last 14 Days", "value": timedelta(days=14), "selected": ("SELECTED" if duration == "14d" else "")}
        valid_durations["20d"] = {"display": "Last 20 Days", "value": timedelta(days=20), "selected": ("SELECTED" if duration == "20d" else "")}
        valid_durations["1m"] = {"display": "Last Month", "value": timedelta(days=365 / 12), "selected": ("SELECTED" if duration == "1m" else "")}
        valid_durations["2m"] = {"display": "Last 2 Months", "value": timedelta(days=2 * 365 / 12), "selected": ("SELECTED" if duration == "2m" else "")}
        valid_durations["3m"] = {"display": "Last 3 Months", "value": timedelta(days=3 * 365 / 12), "selected": ("SELECTED" if duration == "3m" else "")}
        valid_durations["6m"] = {"display": "Last 6 Months", "value": timedelta(days=6 * 365 / 12), "selected": ("SELECTED" if duration == "6m" else "")}
        valid_durations["1y"] = {"display": "Last Year", "value": timedelta(days=365), "selected": ("SELECTED" if duration == "1y" else "")}
        valid_durations["all"] = {"display": "Map Lifetime", "value": 0, "selected": ("SELECTED" if duration == "all" else "")}
        if duration not in valid_durations:
            valid_durations["1d"]["selected"] = "SELECTED"
        valid_sort = OrderedDict()
        valid_sort["count"] = {"display": "Count", "selected": ("SELECTED" if sort == "count" else "")}
        valid_sort["id"] = {"display": "Pokedex Number", "selected": ("SELECTED" if sort == "id" else "")}
        valid_sort["name"] = {"display": "Pokemon Name", "selected": ("SELECTED" if sort == "name" else "")}
        if sort not in valid_sort:
            valid_sort["count"]["selected"] = "SELECTED"
        valid_order = OrderedDict()
        valid_order["asc"] = {"display": "Ascending", "selected": ("SELECTED" if order == "asc" else "")}
        valid_order["desc"] = {"display": "Descending", "selected": ("SELECTED" if order == "desc" else "")}
        if order not in valid_order:
            valid_order["desc"]["selected"] = "SELECTED"
        valid_input = OrderedDict()
        valid_input["duration"] = {"display": "Duration", "items": valid_durations}
        valid_input["sort"] = {"display": "Sort", "items": valid_sort}
        valid_input["order"] = {"display": "Order", "items": valid_order}
        return valid_input

    def get_stats(self):
        return render_template('statistics.html',
                               lat=self.current_location[0],
                               lng=self.current_location[1],
                               gmaps_key=config['GMAPS_KEY'],
                               valid_input=self.get_valid_stat_input()
                               )

    def get_gymdata(self):
        gym_id = request.args.get('id')
        gym = Gym.get_gym(gym_id)

        return jsonify(gym)

    def get_spawndata(self):
        id = request.args.get('id')
        spawn = Pokemon.get_spawnpoint_history(id)

        return jsonify(spawn)

    def get_status(self):
        args = get_args()
        if args.status_page_password is None:
            abort(404)

        return render_template('status.html')

    def post_status(self):
        args = get_args()
        d = {}
        if args.status_page_password is None:
            abort(404)

        if request.form.get('password', None) == args.status_page_password:
            d['login'] = 'ok'
            d['main_workers'] = MainWorker.get_all()
            d['workers'] = WorkerStatus.get_all()
        else:
            d['login'] = 'failed'
        return jsonify(d)


class CustomJSONEncoder(JSONEncoder):

    def default(self, obj):
        try:
            if isinstance(obj, datetime):
                if obj.utcoffset() is not None:
                    obj = obj - obj.utcoffset()
                millis = int(
                    calendar.timegm(obj.timetuple()) * 1000 +
                    obj.microsecond / 1000
                )
                return millis
            iterable = iter(obj)
        except TypeError:
            pass
        else:
            return list(iterable)
        return JSONEncoder.default(self, obj)
