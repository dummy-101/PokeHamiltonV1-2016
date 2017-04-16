#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
import itertools
import calendar
import sys
import gc
import time
import random
import geopy
import cluster
from peewee import SqliteDatabase, InsertQuery, \
    SmallIntegerField, IntegerField, CharField, DoubleField, BooleanField, \
    DateTimeField, fn, DeleteQuery, CompositeKey, FloatField, TextField, JOIN
from playhouse.flask_utils import FlaskDB
from playhouse.pool import PooledMySQLDatabase
from playhouse.shortcuts import RetryOperationalError
from playhouse.migrate import migrate, MySQLMigrator, SqliteMigrator
from datetime import datetime, timedelta
from base64 import b64encode
from cachetools import TTLCache
from cachetools import cached

# balls lol
from random import random
# for geofence
from matplotlib.path import Path
from ast import literal_eval

from . import config
from .utils import get_pokemon_name, get_pokemon_rarity, get_pokemon_types, get_args, get_move_name, get_move_damage, get_move_energy, get_move_type, get_move_name, get_move_damage, get_move_energy, get_move_type, in_radius
from .transform import transform_from_wgs_to_gcj, get_new_coords
from .customLog import printPokemon

alreadyLeveled = False

log = logging.getLogger(__name__)

args = get_args()
flaskDb = FlaskDB()
cache = TTLCache(maxsize=100, ttl=60 * 5)

db_schema_version = 20


class MyRetryDB(RetryOperationalError, PooledMySQLDatabase):
    pass


def init_database(app):
    if args.db_type == 'mysql':
        log.info('Connecting to MySQL database on %s:%i', args.db_host, args.db_port)
        connections = args.db_max_connections
#        if hasattr(args, 'accounts'):
#           connections *= len(args.accounts)
        if hasattr(args, 'workers') and args.workers > 0:
            connections *= args.workers
        db = MyRetryDB(
            args.db_name,
            user=args.db_user,
            password=args.db_pass,
            host=args.db_host,
            port=args.db_port,
            max_connections=connections,
            stale_timeout=300)
    else:
        log.info('Connecting to local SQLite database')
        db = SqliteDatabase(args.db)

    app.config['DATABASE'] = db
    flaskDb.init_app(app)
    if args.clean_timers_data:
        log.info('Cleaning Spawns timer data...')
        Pokemon.clean_timers_data()
    return db


class BaseModel(flaskDb.Model):

    @classmethod
    def get_all(cls):
        results = [m for m in cls.select().dicts()]
        if args.china:
            for result in results:
                result['latitude'], result['longitude'] = \
                    transform_from_wgs_to_gcj(
                        result['latitude'], result['longitude'])
        return results


class Pokemon(BaseModel):
    # We are base64 encoding the ids delivered by the api
    # because they are too big for sqlite to handle
    encounter_id = CharField(primary_key=True, max_length=50)
    spawnpoint_id = CharField(index=True, null=True)
    pokestop_id = CharField(null=True)
    pokemon_id = SmallIntegerField(index=True)
    latitude = DoubleField()
    longitude = DoubleField()
    disappear_time = DateTimeField(index=True)
    individual_attack = SmallIntegerField(null=True)
    individual_defense = SmallIntegerField(null=True)
    individual_stamina = SmallIntegerField(null=True)
    move_1 = SmallIntegerField(null=True)
    move_2 = SmallIntegerField(null=True)
    last_modified = DateTimeField(null=True, index=True, default=datetime.utcnow)
    time_detail = IntegerField(index=True)  # -1 when unknown disappear_time, 0 when predicted, 1 when returned by server
    weight = DoubleField(null=True)
    height = DoubleField(null=True)
    gender = IntegerField(null=True)
    form = SmallIntegerField(null=True)
    previous_id = SmallIntegerField(null=True)
    #cp = SmallIntegerField(null=True)
    #pokemon_level = SmallIntegerField(null=True)
    #trainer_level = SmallIntegerField(null=True)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    @staticmethod
    def get_active(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None):
        query = Pokemon.select()
        if not (swLat and swLng and neLat and neLng):
            query = (query
                     .where(Pokemon.disappear_time > datetime.utcnow())
                     .dicts())
        elif timestamp > 0:
            # If timestamp is known only load modified pokemon
            query = (query
                     .where(((Pokemon.last_modified > datetime.utcfromtimestamp(timestamp / 1000)) &
                             (Pokemon.disappear_time > datetime.utcnow())) &
                            ((Pokemon.latitude >= swLat) &
                             (Pokemon.longitude >= swLng) &
                             (Pokemon.latitude <= neLat) &
                             (Pokemon.longitude <= neLng)))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send Pokemon in view but exclude those within old boundaries. Only send newly uncovered Pokemon.
            query = (query
                     .where(((Pokemon.disappear_time > datetime.utcnow()) &
                            (((Pokemon.latitude >= swLat) &
                              (Pokemon.longitude >= swLng) &
                              (Pokemon.latitude <= neLat) &
                              (Pokemon.longitude <= neLng))) &
                            ~((Pokemon.disappear_time > datetime.utcnow()) &
                              (Pokemon.latitude >= oSwLat) &
                              (Pokemon.longitude >= oSwLng) &
                              (Pokemon.latitude <= oNeLat) &
                              (Pokemon.longitude <= oNeLng))))
                     .dicts())
        else:
            query = (query
                     .where((Pokemon.disappear_time > datetime.utcnow()) &
                            (((Pokemon.latitude >= swLat) &
                              (Pokemon.longitude >= swLng) &
                              (Pokemon.latitude <= neLat) &
                              (Pokemon.longitude <= neLng))))
                     .dicts())

        # Performance: Disable the garbage collector prior to creating a (potentially) large dict with append()
        gc.disable()

        pokemons = []
        for p in query:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
            p['pokemon_rarity'] = get_pokemon_rarity(p['pokemon_id'])
            p['pokemon_types'] = get_pokemon_types(p['pokemon_id'])
            if args.china:
                p['latitude'], p['longitude'] = \
                    transform_from_wgs_to_gcj(p['latitude'], p['longitude'])
            pokemons.append(p)

        # Re-enable the GC.
        gc.enable()

        return pokemons

    @staticmethod
    def get_active_by_id(ids, swLat, swLng, neLat, neLng):
        if not (swLat and swLng and neLat and neLng):
            query = (Pokemon
                     .select()
                     .where((Pokemon.pokemon_id << ids) &
                            (Pokemon.disappear_time > datetime.utcnow()))
                     .dicts())
        else:
            query = (Pokemon
                     .select()
                     .where((Pokemon.pokemon_id << ids) &
                            (Pokemon.disappear_time > datetime.utcnow()) &
                            (Pokemon.latitude >= swLat) &
                            (Pokemon.longitude >= swLng) &
                            (Pokemon.latitude <= neLat) &
                            (Pokemon.longitude <= neLng))
                     .dicts())

        # Performance: Disable the garbage collector prior to creating a (potentially) large dict with append()
        gc.disable()

        pokemons = []
        for p in query:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
            p['pokemon_rarity'] = get_pokemon_rarity(p['pokemon_id'])
            p['pokemon_types'] = get_pokemon_types(p['pokemon_id'])
            if args.china:
                p['latitude'], p['longitude'] = \
                    transform_from_wgs_to_gcj(p['latitude'], p['longitude'])
            pokemons.append(p)

        # Re-enable the GC.
        gc.enable()

        return pokemons

    @classmethod
    @cached(cache)
    def get_seen(cls, timediff):
        if timediff:
            timediff = datetime.utcnow() - timediff
        pokemon_count_query = (Pokemon
                               .select(Pokemon.pokemon_id,
                                       fn.COUNT(Pokemon.pokemon_id).alias('count'),
                                       fn.MAX(Pokemon.disappear_time).alias('lastappeared')
                                       )
                               .where(Pokemon.disappear_time > timediff)
                               .group_by(Pokemon.pokemon_id)
                               .alias('counttable')
                               )
        query = (Pokemon
                 .select(Pokemon.pokemon_id,
                         Pokemon.disappear_time,
                         Pokemon.latitude,
                         Pokemon.longitude,
                         pokemon_count_query.c.count)
                 .join(pokemon_count_query, on=(Pokemon.pokemon_id == pokemon_count_query.c.pokemon_id))
                 .distinct()
                 .where(Pokemon.disappear_time == pokemon_count_query.c.lastappeared)
                 .dicts()
                 )

        # Performance: Disable the garbage collector prior to creating a (potentially) large dict with append()
        gc.disable()

        pokemons = []
        total = 0
        for p in query:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
            pokemons.append(p)
            total += p['count']

        # Re-enable the GC.
        gc.enable()

        return {'pokemon': pokemons, 'total': total}

    @classmethod
    def get_appearances(cls, pokemon_id, timediff):
        '''
        :param pokemon_id: id of pokemon that we need appearances for
        :param timediff: limiting period of the selection
        :return: list of  pokemon  appearances over a selected period (excluding lured appearances)
        '''
        if timediff:
            timediff = datetime.utcnow() - timediff
        query = (Pokemon
                 .select(Pokemon.latitude, Pokemon.longitude, Pokemon.pokemon_id, fn.Count(Pokemon.spawnpoint_id).alias('count'), Pokemon.spawnpoint_id)
                 .where((Pokemon.pokemon_id == pokemon_id) &
                        (Pokemon.disappear_time > timediff)
                        )
                 .group_by(Pokemon.latitude, Pokemon.longitude, Pokemon.pokemon_id, Pokemon.spawnpoint_id)
                 .dicts()
                 )

        return list(query)

    @classmethod
    def get_appearances_times_by_spawnpoint(cls, pokemon_id, spawnpoint_id, timediff):
        '''
        :param pokemon_id: id of pokemon that we need appearances times for
        :param spawnpoint_id: spawnpoing id we need appearances times for
        :param timediff: limiting period of the selection
        :return: list of time appearances over a selected period
        '''
        if timediff:
            timediff = datetime.utcnow() - timediff
        query = (Pokemon
                 .select(Pokemon.disappear_time)
                 .where((Pokemon.pokemon_id == pokemon_id) &
                        (Pokemon.spawnpoint_id == spawnpoint_id) &
                        (Pokemon.disappear_time > timediff)
                        )
                 .order_by(Pokemon.disappear_time.asc())
                 .tuples()
                 )

        return list(itertools.chain(*query))

    @classmethod
    def get_spawn_time(cls, disappear_time):
        return (disappear_time + 1800) % 3600

    @classmethod
    def clean_timers_data(cls):
        query = Pokemon.update(time_detail=-1).where(Pokemon.time_detail == 1)
        query.execute()

    @classmethod
    def predict_disappear_time(cls, spawnpoint_id):
        now = datetime.utcnow()
        predicted = -1

        query = (Pokemon
                 .select(Pokemon.disappear_time)
                 .where((Pokemon.spawnpoint_id == spawnpoint_id) &
                        (Pokemon.time_detail == 1))
                 .order_by(Pokemon.last_modified.desc())
                 .limit(1)).dicts()

        temp = list(query)

        log.debug("Found %d entrie(s) in db as to predict disappear_time" % (len(temp)))

        if len(temp) > 0:
            disappear_time = temp[0]['disappear_time']

            predicted = now.replace(minute=disappear_time.minute, second=disappear_time.second)

            if now > predicted:
                predicted = predicted + timedelta(hours=1)

            log.debug("Predicted datetime %s " % (predicted.strftime("%Y-%m-%d %H:%M:%S")))
        return predicted

    @classmethod
    def get_spawnpoints(cls, swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None):
        subquery = Pokemon.select(Pokemon.spawnpoint_id.alias('spawn_id'), fn.Max(Pokemon.time_detail).alias('td')).group_by(Pokemon.spawnpoint_id).alias("derived")
        query = (Pokemon.select(Pokemon.latitude, Pokemon.longitude, Pokemon.spawnpoint_id, Pokemon.disappear_time, Pokemon.last_modified, subquery.c.td.alias('time_detail'), ((Pokemon.disappear_time.minute * 60) + Pokemon.disappear_time.second).alias('time'), fn.Count(Pokemon.spawnpoint_id).alias('count'))).where(Pokemon.spawnpoint_id.is_null(False))

        query = query.join(subquery, on=(subquery.c.spawn_id == Pokemon.spawnpoint_id))
        if timestamp > 0:
            query = (query
                     .where(((Pokemon.last_modified > datetime.utcfromtimestamp(timestamp / 1000))) &
                            ((Pokemon.latitude >= swLat) &
                             (Pokemon.longitude >= swLng) &
                             (Pokemon.latitude <= neLat) &
                             (Pokemon.longitude <= neLng)))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send spawnpoints in view but exclude those within old boundaries. Only send newly uncovered spawnpoints.
            query = (query
                     .where((((Pokemon.latitude >= swLat) &
                              (Pokemon.longitude >= swLng) &
                              (Pokemon.latitude <= neLat) &
                              (Pokemon.longitude <= neLng))) &
                            ~((Pokemon.latitude >= oSwLat) &
                              (Pokemon.longitude >= oSwLng) &
                              (Pokemon.latitude <= oNeLat) &
                              (Pokemon.longitude <= oNeLng)
                              ))
                     .dicts())
        elif swLat and swLng and neLat and neLng:
            query = (query
                     .where((Pokemon.latitude <= neLat) &
                            (Pokemon.latitude >= swLat) &
                            (Pokemon.longitude >= swLng) &
                            (Pokemon.longitude <= neLng)
                            ))

        query = query.group_by(Pokemon.latitude, Pokemon.longitude, Pokemon.spawnpoint_id)

        queryDict = query.dicts()
        spawnpoints = {}

        for sp in queryDict:
            key = sp['spawnpoint_id']
            disappear_time = cls.get_spawn_time(sp.pop('time'))
            count = int(sp['count'])

            if key not in spawnpoints:
                spawnpoints[key] = sp

            if (sp['disappear_time'] - sp['last_modified']) > timedelta(minutes=30):
                spawnpoints[key]['special'] = True

            if 'time' not in spawnpoints[key] or count >= spawnpoints[key]['count']:
                spawnpoints[key]['time'] = disappear_time
                spawnpoints[key]['count'] = count

        for sp in spawnpoints.values():
            del sp['count']

        return list(spawnpoints.values())

    @classmethod
    def get_spawnpoints_in_hex(cls, center, steps):
        log.info('Finding spawn points {} steps away'.format(steps))

        n, e, s, w = hex_bounds(center, steps)

        query = (Pokemon
                 .select(Pokemon.latitude.alias('lat'),
                         Pokemon.longitude.alias('lng'),
                         ((Pokemon.disappear_time.minute * 60) + Pokemon.disappear_time.second).alias('time'),
                         Pokemon.spawnpoint_id
                         ))
        subquery = Pokemon.select(Pokemon.spawnpoint_id.alias('spawn_id'), fn.Max(Pokemon.time_detail).alias('td')).group_by(Pokemon.spawnpoint_id).alias("derived")
        query = query.join(subquery, on=(subquery.c.spawn_id == Pokemon.spawnpoint_id))

        query = (query.where((Pokemon.latitude <= n) &
                             (Pokemon.latitude >= s) &
                             (Pokemon.longitude >= w) &
                             (Pokemon.longitude <= e) &
                             (Pokemon.spawnpoint_id.is_null(False))
                             ))
        # CONFLICT: should we keep this code instead of the if below
        # query = query.group_by(Pokemon.spawnpoint_id)

        # Sqlite doesn't support distinct on columns.
        if args.db_type == 'mysql':
            query = query.distinct(Pokemon.spawnpoint_id)
        else:
            query = query.group_by(Pokemon.spawnpoint_id)

        s = list(query.dicts())

        # The distance between scan circles of radius 70 in a hex is 121.2436
        # steps - 1 to account for the center circle then add 70 for the edge
        step_distance = ((steps - 1) * 121.2436) + 70
        # Compare spawnpoint list to a circle with radius steps * 120
        # Uses the direct geopy distance between the center and the spawnpoint.
        filtered = []

        for idx, sp in enumerate(s):
            if geopy.distance.distance(center, (sp['lat'], sp['lng'])).meters <= step_distance:
                filtered.append(s[idx])

        # at this point, 'time' is DISAPPEARANCE time, we're going to morph it to APPEARANCE time
        for location in filtered:
            # examples: time    shifted
            #           0       (   0 + 2700) = 2700 % 3600 = 2700 (0th minute to 45th minute, 15 minutes prior to appearance as time wraps around the hour)
            #           1800    (1800 + 2700) = 4500 % 3600 =  900 (30th minute, moved to arrive at 15th minute)
            # todo: this DOES NOT ACCOUNT for pokemons that appear sooner and live longer, but you'll _always_ have at least 15 minutes, so it works well enough
            location['time'] = cls.get_spawn_time(location['time'])

        if args.sscluster:
            filtered = cluster.main(filtered)

        return filtered

    @classmethod
    def get_spawn_history(cls, spawnpoint_id):
        query = (Pokemon
                 .select(fn.Count(Pokemon.pokemon_id).alias('count'), Pokemon.pokemon_id)
                 .where((Pokemon.spawnpoint_id == spawnpoint_id))
                 .group_by(Pokemon.pokemon_id)
                 .order_by(Pokemon.pokemon_id)
                 .dicts())

        return list(query)

    @staticmethod
    def get_spawnpoint_history(id):
        result = {}
        result['pokemon'] = []
        pokemon = (Pokemon
                   .select(Pokemon.pokemon_id,
                           Pokemon.disappear_time,
                           Pokemon.individual_attack,
                           Pokemon.individual_defense,
                           Pokemon.individual_stamina,
                           Pokemon.move_1,
                           Pokemon.move_2)
                   .where(Pokemon.spawnpoint_id == id)
                   .order_by(Pokemon.disappear_time.desc())
                   .dicts())
        for p in pokemon:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])

            if p['move_1'] is not None:
                p['move_1_name'] = get_move_name(p['move_1'])
                p['move_1_damage'] = get_move_damage(p['move_1'])
                p['move_1_energy'] = get_move_energy(p['move_1'])
                p['move_1_type'] = get_move_type(p['move_1'])
            if p['move_2'] is not None:
                p['move_2_name'] = get_move_name(p['move_2'])
                p['move_2_damage'] = get_move_damage(p['move_2'])
                p['move_2_energy'] = get_move_energy(p['move_2'])
                p['move_2_type'] = get_move_type(p['move_2'])
            result['pokemon'].append(p)
        return result


    @classmethod
    def get_spawn_history2(cls, spawnpoint_id):
        lastday = datetime.utcnow() - timedelta(hours=24)
        query = (Pokemon.select(
                fn.Count(Pokemon.pokemon_id).alias('count'),
                Pokemon.pokemon_id)
            .where(
                (Pokemon.spawnpoint_id == spawnpoint_id) &
                (Pokemon.disappear_time > lastday))
            .group_by(Pokemon.pokemon_id)
            .order_by(-SQL('count'))
            .dicts())

        return list(query)


class Pokestop(BaseModel):
    pokestop_id = CharField(primary_key=True, max_length=50)
    enabled = BooleanField()
    latitude = DoubleField()
    longitude = DoubleField()
    last_modified = DateTimeField(index=True)
    lure_expiration = DateTimeField(null=True, index=True)
    active_fort_modifier = CharField(max_length=50, null=True, index=True)
    last_updated = DateTimeField(null=True, index=True, default=datetime.utcnow)
    player_lure = CharField(index=True, null=True)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    @staticmethod
    def get_stops(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None, lured=False):

        #query = Pokestop.select(Pokestop.active_fort_modifier, Pokestop.enabled, Pokestop.latitude, Pokestop.longitude, Pokestop.last_modified, Pokestop.lure_expiration, Pokestop.pokestop_id)
        query = (Pokestop.select(Pokestop.active_fort_modifier, Pokestop.enabled, Pokestop.latitude, Pokestop.longitude, Pokestop.last_modified, Pokestop.lure_expiration, Pokestop.player_lure, Pokestop.pokestop_id, PokestopDetails.name, PokestopDetails.description, PokestopDetails.image_url, PokestopDetails.last_scanned) .join(PokestopDetails, JOIN.LEFT_OUTER, on=(PokestopDetails.pokestop_id == Pokestop.pokestop_id)) .dicts())

        if not (swLat and swLng and neLat and neLng):
            query = (query
                     .dicts())
        elif timestamp > 0:
            query = (query
                     .where(((Pokestop.last_updated > datetime.utcfromtimestamp(timestamp / 1000))) &
                            (Pokestop.latitude >= swLat) &
                            (Pokestop.longitude >= swLng) &
                            (Pokestop.latitude <= neLat) &
                            (Pokestop.longitude <= neLng))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng and lured:
            query = (query
                     .where((((Pokestop.latitude >= swLat) &
                              (Pokestop.longitude >= swLng) &
                              (Pokestop.latitude <= neLat) &
                              (Pokestop.longitude <= neLng)) &
                             (Pokestop.active_fort_modifier.is_null(False))) &
                            ~((Pokestop.latitude >= oSwLat) &
                              (Pokestop.longitude >= oSwLng) &
                              (Pokestop.latitude <= oNeLat) &
                              (Pokestop.longitude <= oNeLng)) &
                             (Pokestop.active_fort_modifier.is_null(False)))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send stops in view but exclude those within old boundaries. Only send newly uncovered stops.
            query = (query
                     .where(((Pokestop.latitude >= swLat) &
                             (Pokestop.longitude >= swLng) &
                             (Pokestop.latitude <= neLat) &
                             (Pokestop.longitude <= neLng)) &
                            ~((Pokestop.latitude >= oSwLat) &
                              (Pokestop.longitude >= oSwLng) &
                              (Pokestop.latitude <= oNeLat) &
                              (Pokestop.longitude <= oNeLng)))
                     .dicts())
        elif lured:
            query = (query
                     .where(((Pokestop.last_updated > datetime.utcfromtimestamp(timestamp / 1000))) &
                            ((Pokestop.latitude >= swLat) &
                             (Pokestop.longitude >= swLng) &
                             (Pokestop.latitude <= neLat) &
                             (Pokestop.longitude <= neLng)) &
                            (Pokestop.active_fort_modifier.is_null(False)))
                     .dicts())

        else:
            query = (query
                     .where((Pokestop.latitude >= swLat) &
                            (Pokestop.longitude >= swLng) &
                            (Pokestop.latitude <= neLat) &
                            (Pokestop.longitude <= neLng))
                     .dicts())

        # Performance: Disable the garbage collector prior to creating a (potentially) large dict with append()
        gc.disable()

        pokestops = []
        for p in query:
            if args.china:
                p['latitude'], p['longitude'] = \
                    transform_from_wgs_to_gcj(p['latitude'], p['longitude'])
            pokestops.append(p)

        # Re-enable the GC.
        gc.enable()

        return pokestops


class Gym(BaseModel):
    UNCONTESTED = 0
    TEAM_MYSTIC = 1
    TEAM_VALOR = 2
    TEAM_INSTINCT = 3

    gym_id = CharField(primary_key=True, max_length=50)
    team_id = SmallIntegerField()
    guard_pokemon_id = SmallIntegerField()
    gym_points = IntegerField()
    enabled = BooleanField()
    latitude = DoubleField()
    longitude = DoubleField()
    last_modified = DateTimeField(index=True)
    last_scanned = DateTimeField(default=datetime.utcnow, index=True)
    is_active = SmallIntegerField(null=True)
    train_battle = SmallIntegerField(null=True)

    class Meta:
        indexes = ((('latitude', 'longitude'), False),)

    @staticmethod
    def get_gyms(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None):
        if not (swLat and swLng and neLat and neLng):
            results = (Gym
                       .select()
                       .dicts())
        elif timestamp > 0:
            # If timestamp is known only send last scanned Gyms.
            results = (Gym
                       .select()
                       .where(((Gym.last_scanned > datetime.utcfromtimestamp(timestamp / 1000)) &
                              (Gym.latitude >= swLat) &
                              (Gym.longitude >= swLng) &
                              (Gym.latitude <= neLat) &
                              (Gym.longitude <= neLng)))
                       .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send gyms in view but exclude those within old boundaries. Only send newly uncovered gyms.
            results = (Gym
                       .select()
                       .where(((Gym.latitude >= swLat) &
                               (Gym.longitude >= swLng) &
                               (Gym.latitude <= neLat) &
                               (Gym.longitude <= neLng)) &
                              ~((Gym.latitude >= oSwLat) &
                                (Gym.longitude >= oSwLng) &
                                (Gym.latitude <= oNeLat) &
                                (Gym.longitude <= oNeLng)))
                       .dicts())

        else:
            results = (Gym
                       .select()
                       .where((Gym.latitude >= swLat) &
                              (Gym.longitude >= swLng) &
                              (Gym.latitude <= neLat) &
                              (Gym.longitude <= neLng))
                       .dicts())

        # Performance: Disable the garbage collector prior to creating a (potentially) large dict with append()
        gc.disable()

        gyms = {}
        gym_ids = []
        for g in results:
            g['name'] = None
            g['pokemon'] = []
            gyms[g['gym_id']] = g
            gym_ids.append(g['gym_id'])

        if len(gym_ids) > 0:
            pokemon = (GymMember
                       .select(
                           GymMember.gym_id,
                           GymPokemon.cp.alias('pokemon_cp'),
                           GymPokemon.pokemon_id,
                           Trainer.name.alias('trainer_name'),
                           Trainer.level.alias('trainer_level'))
                       .join(Gym, on=(GymMember.gym_id == Gym.gym_id))
                       .join(GymPokemon, on=(GymMember.pokemon_uid == GymPokemon.pokemon_uid))
                       .join(Trainer, on=(GymPokemon.trainer_name == Trainer.name))
                       .where(GymMember.gym_id << gym_ids)
                       .where(GymMember.last_scanned > Gym.last_modified)
                       .order_by(GymMember.gym_id, GymPokemon.cp)
                       .dicts())

            for p in pokemon:
                p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])
                gyms[p['gym_id']]['pokemon'].append(p)

            details = (GymDetails
                       .select(
                           GymDetails.gym_id,
                           GymDetails.name,
                           GymDetails.description,
                           GymDetails.url)
                       .where(GymDetails.gym_id << gym_ids)
                       .dicts())

            for d in details:
                gyms[d['gym_id']]['name'] = d['name']
                gyms[d['gym_id']]['description'] = d['description']
                gyms[d['gym_id']]['url'] = d['url']

        # Re-enable the GC.
        gc.enable()

        return gyms

    @staticmethod
    def get_gym(id):
        result = (Gym
                  .select(Gym.gym_id,
                          Gym.team_id,
                          GymDetails.name,
                          GymDetails.description,
                          GymDetails.url,
                          Gym.guard_pokemon_id,
                          Gym.gym_points,
                          Gym.latitude,
                          Gym.longitude,
                          Gym.last_modified,
                          Gym.last_scanned,
                          Gym.is_active,
                          Gym.train_battle)
                  .join(GymDetails, JOIN.LEFT_OUTER, on=(Gym.gym_id == GymDetails.gym_id))
                  .where(Gym.gym_id == id)
                  .dicts()
                  .get())

        result['guard_pokemon_name'] = get_pokemon_name(result['guard_pokemon_id']) if result['guard_pokemon_id'] else ''
        result['pokemon'] = []

        pokemon = (GymMember
                   .select(GymPokemon.cp.alias('pokemon_cp'),
                           GymPokemon.pokemon_id,
                           GymPokemon.pokemon_uid,
                           GymPokemon.move_1,
                           GymPokemon.move_2,
                           GymPokemon.iv_attack,
                           GymPokemon.iv_defense,
                           GymPokemon.iv_stamina,
                           Trainer.name.alias('trainer_name'),
                           Trainer.level.alias('trainer_level'))
                   .join(Gym, on=(GymMember.gym_id == Gym.gym_id))
                   .join(GymPokemon, on=(GymMember.pokemon_uid == GymPokemon.pokemon_uid))
                   .join(Trainer, on=(GymPokemon.trainer_name == Trainer.name))
                   .where(GymMember.gym_id == id)
                   .where(GymMember.last_scanned > Gym.last_modified)
                   .order_by(GymPokemon.cp.desc())
                   .dicts())

        for p in pokemon:
            p['pokemon_name'] = get_pokemon_name(p['pokemon_id'])

            p['move_1_name'] = get_move_name(p['move_1'])
            p['move_1_damage'] = get_move_damage(p['move_1'])
            p['move_1_energy'] = get_move_energy(p['move_1'])
            p['move_1_type'] = get_move_type(p['move_1'])

            p['move_2_name'] = get_move_name(p['move_2'])
            p['move_2_damage'] = get_move_damage(p['move_2'])
            p['move_2_energy'] = get_move_energy(p['move_2'])
            p['move_2_type'] = get_move_type(p['move_2'])

            result['pokemon'].append(p)

        return result


class ScannedLocation(BaseModel):
    latitude = DoubleField()
    longitude = DoubleField()
    username = CharField()
    last_modified = DateTimeField(index=True, default=datetime.utcnow)

    class Meta:
        primary_key = CompositeKey('latitude', 'longitude')

    @staticmethod
    def get_recent(swLat, swLng, neLat, neLng, timestamp=0, oSwLat=None, oSwLng=None, oNeLat=None, oNeLng=None):
        activeTime = (datetime.utcnow() - timedelta(minutes=15))
        if timestamp > 0:
            query = (ScannedLocation
                     .select()
                     .where(((ScannedLocation.last_modified >= datetime.utcfromtimestamp(timestamp / 1000))) &
                            (ScannedLocation.latitude >= swLat) &
                            (ScannedLocation.longitude >= swLng) &
                            (ScannedLocation.latitude <= neLat) &
                            (ScannedLocation.longitude <= neLng))
                     .dicts())
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send scannedlocations in view but exclude those within old boundaries. Only send newly uncovered scannedlocations.
            query = (ScannedLocation
                     .select()
                     .where((((ScannedLocation.last_modified >= activeTime)) &
                             (ScannedLocation.latitude >= swLat) &
                             (ScannedLocation.longitude >= swLng) &
                             (ScannedLocation.latitude <= neLat) &
                             (ScannedLocation.longitude <= neLng)) &
                            ~(((ScannedLocation.last_modified >= activeTime)) &
                              (ScannedLocation.latitude >= oSwLat) &
                              (ScannedLocation.longitude >= oSwLng) &
                              (ScannedLocation.latitude <= oNeLat) &
                              (ScannedLocation.longitude <= oNeLng)))
                     .dicts())
        else:
            query = (ScannedLocation
                     .select()
                     .where((ScannedLocation.last_modified >= activeTime) &
                            (ScannedLocation.latitude >= swLat) &
                            (ScannedLocation.longitude >= swLng) &
                            (ScannedLocation.latitude <= neLat) &
                            (ScannedLocation.longitude <= neLng))
                     .order_by(ScannedLocation.last_modified.asc())
                     .dicts())

        return list(query)


class MainWorker(BaseModel):
    worker_name = CharField(primary_key=True, max_length=50)
    message = CharField()
    method = CharField(max_length=50)
    last_modified = DateTimeField(index=True)


class WorkerStatus(BaseModel):
    username = CharField(primary_key=True, max_length=50)
    worker_name = CharField()
    success = IntegerField()
    fail = IntegerField()
    no_items = IntegerField()
    captchas = IntegerField(default=0)
    skip = IntegerField()
    last_modified = DateTimeField(index=True)
    message = CharField(max_length=255)

    @staticmethod
    def get_recent():
        query = (WorkerStatus
                 .select()
                 .where((WorkerStatus.last_modified >=
                        (datetime.utcnow() - timedelta(minutes=5))))
                 .order_by(WorkerStatus.username)
                 .dicts())

        status = []
        for s in query:
            status.append(s)

        return status


class Versions(flaskDb.Model):
    key = CharField()
    val = SmallIntegerField()

    class Meta:
        primary_key = False


class GymMember(BaseModel):
    gym_id = CharField(index=True)
    pokemon_uid = CharField(index=True)
    last_scanned = DateTimeField(default=datetime.utcnow, index=True)

    class Meta:
        primary_key = False


class GymPokemon(BaseModel):
    pokemon_uid = CharField(primary_key=True, max_length=50)
    pokemon_id = SmallIntegerField()
    cp = SmallIntegerField()
    trainer_name = CharField(index=True)
    num_upgrades = SmallIntegerField(null=True)
    move_1 = SmallIntegerField(null=True)
    move_2 = SmallIntegerField(null=True)
    height = FloatField(null=True)
    weight = FloatField(null=True)
    stamina = SmallIntegerField(null=True)
    stamina_max = SmallIntegerField(null=True)
    cp_multiplier = FloatField(null=True)
    additional_cp_multiplier = FloatField(null=True)
    iv_defense = SmallIntegerField(null=True)
    iv_stamina = SmallIntegerField(null=True)
    iv_attack = SmallIntegerField(null=True)
    last_seen = DateTimeField(default=datetime.utcnow)


class Trainer(BaseModel):
    name = CharField(primary_key=True, max_length=50)
    team = SmallIntegerField()
    level = SmallIntegerField()
    last_seen = DateTimeField(default=datetime.utcnow)


class GymDetails(BaseModel):
    gym_id = CharField(primary_key=True, max_length=50)
    name = CharField()
    description = TextField(null=True, default="")
    url = CharField()
    last_scanned = DateTimeField(default=datetime.utcnow)

class PokestopDetails(BaseModel):
    pokestop_id = CharField(primary_key=True, index=True, max_length=50)
    name = CharField()
    description = TextField(null=True, default="")
    image_url = TextField(null=True, default="")
    last_scanned = DateTimeField(default=datetime.utcnow)

def hex_bounds(center, steps):
    # Make a box that is (70m * step_limit * 2) + 70m away from the center point
    # Rationale is that you need to travel
    sp_dist = 0.07 * 2 * steps
    n = get_new_coords(center, sp_dist, 0)[0]
    e = get_new_coords(center, sp_dist, 90)[1]
    s = get_new_coords(center, sp_dist, 180)[0]
    w = get_new_coords(center, sp_dist, 270)[1]
    return (n, e, s, w)

def geofence(step_location, geofence_file, forbidden=False):
    geofence = []
    with open(geofence_file) as f:
        for line in f:
            if len(line.strip()) == 0 or line.startswith('#'):
                continue
            geofence.append(literal_eval(line.strip()))
        # if forbidden:
            # log.info('Loaded %d geofence-forbidden coordinates. ' +
                    #  'Applying...', len(geofence))
        # else:
            # log.info('Loaded %d geofence coordinates. Applying...',
                    # len(geofence))
    # log.info(geofence)
    p = Path(geofence)
    step_location_geofenced = []
    result_x, result_y, result_z = step_location
    if p.contains_point([step_location[0], step_location[1]]) ^ forbidden:
        step_location_geofenced.append((result_x, result_y, result_z))
        # log.warning('FOUND IN THE GEOFENCE, LURING: %s, %s', result_x, result_y)
    return step_location_geofenced


def construct_pokemon_dict(pokemons, p, encounter_result, d_t, api, nextLevel, currentExp, level, time_detail=-1, lure_info=None):
    dittomons = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251]
    #dittomons = [16, 19, 41, 129, 161, 163, 193]

    if lure_info is None:
        encounter_id = p['encounter_id']
        spawnpoint_id = p['spawn_point_id']
        pokestop_id = None
        pokemon_id = p['pokemon_data']['pokemon_id']
    else:
        encounter_id = lure_info['encounter_id']
        spawnpoint_id = None
        pokestop_id = b64encode(str(p['id']))
        pokemon_id = lure_info['active_pokemon_id']

    pokemons[encounter_id] = {
        'encounter_id': b64encode(str(encounter_id)),
        'spawnpoint_id': spawnpoint_id,
        'pokestop_id': pokestop_id,
        'pokemon_id': pokemon_id,
        'latitude': p['latitude'],
        'longitude': p['longitude'],
        'disappear_time': d_t,
        'time_detail': time_detail,
        'individual_attack': None,
        'individual_defense': None,
        'individual_stamina': None,
        'move_1': None,
        'move_2': None,
        'height': None,
        'weight': None,
        'gender': None,
        'form': None,
        'previous_id': None, #'previous_id': p['pokemon_data']['pokemon_id']
        #'cp': None,
        #'pokemon_level': None,
        #'trainer_level': None,
    }

    pokemon_info = None
    if encounter_result is not None:
        #if lure_info is not None and encounter_result['responses']['DISK_ENCOUNTER']['result'] == 1:
        #    pokemon_info = encounter_result['responses']['DISK_ENCOUNTER']['pokemon_data']

        #if lure_info is None and 'wild_pokemon' in encounter_result['responses']['ENCOUNTER']:
        #    pokemon_info = encounter_result['responses']['ENCOUNTER']['wild_pokemon']['pokemon_data']

        try:
            if lure_info is not None and encounter_result['responses']['DISK_ENCOUNTER']['result'] == 1:
                pokemon_info = encounter_result['responses']['DISK_ENCOUNTER']['pokemon_data']
            elif 'wild_pokemon' in encounter_result['responses']['ENCOUNTER']:
                pokemon_info = encounter_result['responses']['ENCOUNTER']['wild_pokemon']['pokemon_data']
        except KeyError:
            log.warning('KEY ERROR!: %s', encounter_result)

    if pokemon_info is not None:  # if successful encounter:

        #Encounter
        attack = pokemon_info.get('individual_attack', 0)
        defense = pokemon_info.get('individual_defense', 0)
        stamina = pokemon_info.get('individual_stamina', 0)
        #cp = pokemon_info["cp"]
        #pokemon_level = calc_pokemon_level(pokemon_info)
        #trainer_level = get_player_level(encounter_result)
        pokemons[encounter_id].update({
            'individual_attack': attack,
            'individual_defense': defense,
            'individual_stamina': stamina,
            'move_1': pokemon_info['move_1'],
            'move_2': pokemon_info['move_2'],
            'height': pokemon_info['height_m'],
            'weight': pokemon_info['weight_kg'],
            'gender': pokemon_info['pokemon_display']['gender'],
            #'cp': cp,
            #'pokemon_level': pokemon_level,
            #'trainer_level': trainer_level,
        })
        #log.warning('Pokemon CP is %s', cp)
        #FormDBLogging
        if (pokemon_info['pokemon_id'] == 201 and 'form' in pokemon_info['pokemon_display']):
            pokemons[p['encounter_id']].update({
                'form': pokemon_info['pokemon_display']['form']
            })
            log.exception('********UNOWN FORM: %s********', pokemon_info['pokemon_display']['form'])

        #CATCH POKEMON
        pokeball_count = 0
        greatball_count = 0
        ultraball_count = 0
        current_ball = 1
        inventory = encounter_result['responses']['GET_INVENTORY']['inventory_delta']['inventory_items']
        for item in inventory:
            inventory_item_data = item['inventory_item_data']

            if not inventory_item_data:
                continue

            if 'item' in inventory_item_data and inventory_item_data['item']['item_id'] is 1:
                pokeball_count = inventory_item_data['item'].get('count', 0)
                #log.warning('@@@INVENTORY@@@ there are %s regular pokeballs', pokeball_count)
            elif 'item' in inventory_item_data and inventory_item_data['item']['item_id'] is 2:
                greatball_count = inventory_item_data['item'].get('count', 0)
                #log.warning('@@@INVENTORY@@@ there are %s great pokeballs', greatball_count)
            elif 'item' in inventory_item_data and inventory_item_data['item']['item_id'] is 3:
                ultraball_count = inventory_item_data['item'].get('count', 0)
                #log.warning('@@@INVENTORY@@@ there are %s ultra pokeballs', ultraball_count)
        #log.warning('@@@INVENTORY@@@ there are [%s regular] [%s great] [%s ultra] pokeballs', pokeball_count, greatball_count, ultraball_count)
        
        catch_pid = None
        if ultraball_count == 0 and greatball_count == 0 and pokeball_count == 0:
            log.warning('***CATCHING DUDES***No balls! Not gonna try and catch')
            catch_pid = 'blueballs'
        #TO DO WORK DITTO LURE IN
        # Now catch it if it's a ditto-mon 
        if args.ditto is True and int(level) < int(args.level_cap):
            if lure_info is None and p['pokemon_data']['pokemon_id'] in dittomons:
                log.warning('***CATCHING DUDES***Ditto pokemon found, catching - EncID:%s', b64encode(str(p['encounter_id'])))
                while catch_pid is None:
                    time.sleep(2.10)
                    random_throw = 1.5 + 0.25 * random()
                    random_spin = 0.8 + 0.1 * random()
                    req = api.create_request()
                    catch_result = req.check_challenge()
                    catch_result = req.get_hatched_eggs()
                    catch_result = req.get_inventory()
                    catch_result = req.check_awarded_badges()
                    catch_result = req.download_settings()
                    catch_result = req.get_buddy_walked()
                    catch_result = req.catch_pokemon(encounter_id=p['encounter_id'],
                                                     pokeball=current_ball,
                                                     normalized_reticle_size=random_throw,
                                                     spawn_point_id=p['spawn_point_id'],
                                                     hit_pokemon=1,
                                                     spin_modifier=random_spin,
                                                     normalized_hit_position=1.0)
                    catch_result = req.call()
                    # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                    captcha_url = catch_result['responses']['CHECK_CHALLENGE']['challenge_url']
                    if len(captcha_url) > 1:
                        log.warning('fuck, captcha\'d, **DURING CATCHING** now return Bad Scan so that this can be re-scanned')
                        return {
                            'count': 0,
                            'gyms': gyms,
                            'bad_scan': True,
                            'captcha': True,
                            'failed': 'catching'
                        }
                    try:
                        catch_result['responses']['CATCH_POKEMON']['status']
                    except Exception as e:
                        #log.exception('***CATCHING DUDES***Catch request failed: %s', e)
                        catch_result = False
                    if not catch_result:
                        log.warning('***CATCHING DUDES***Catch request failed!! Waiting 10 then trying to catch again')
                        catch_response = 2
                        time.sleep(10)
                    else:
                        # log.warning('***CATCHING DUDES*** IMPORDANT %s', catch_result['responses']['CATCH_POKEMON']['status'])
                        catch_response = catch_result['responses']['CATCH_POKEMON']['status']
                    if catch_response is 1:
                        log.warning('***CATCHING DUDES***Catch SUCC-cess')
                        awardedExp = 0
                        for number in catch_result['responses']['CATCH_POKEMON']['capture_award']['xp']:
                            awardedExp = awardedExp + number
                        #log.warning('$$$PLAYERSTATS$$$ xp is : %s', awardedExp)
                        oldExp = currentExp
                        currentExp = currentExp + awardedExp
                        log.warning('$$$PLAYERSTATS$$$ Caught pokemon so increased XP by %s, old XP was, %s now is %s, next level at %s', awardedExp, oldExp, currentExp, nextLevel)
                        if currentExp == nextLevel or currentExp > nextLevel:
                            log.warning('$$$PLAYERSTATS$$$ +++++++++++++LEVEL UP+++++++++++++ DETECTED OH SHIT')
                            alreadyleveled = True
                            levelup = level + 1
                            levelStatus = None
                            while levelStatus is None:
                                req = api.create_request()
                                levelResponse = req.level_up_rewards(level=levelup)
                                time.sleep(1)
                                levelResponse = req.call()
                                # log.warning('$$$LEVELUP$$$ %s', levelResponse['responses'])
                                levelStatus = levelResponse['responses']['LEVEL_UP_REWARDS']['result']
                                if levelStatus == 0:
                                    log.exception('$$$PLAYERSTATS$$$ SHIT IT\'S UNSET WHAT DOES THAT MEAN')
                                elif levelStatus == 1:
                                    log.warning('$$$PLAYERSTATS$$$ Level up rewards SUCC CESS')
                                elif levelStatus == 2:
                                    log.exception('$$$PLAYERSTATS$$$ Level up reward ALREADY TAKEN the code is BROKE')
                                else:
                                    log.exception('$$$PLAYERSTATS$$$ UNKNOWN, SHIT IS BLANK')
                        catch_pid = catch_result['responses']['CATCH_POKEMON']['captured_pokemon_id']
                    #    log.warning('***CATCHING DUDES***PID:%s', catch_pid)
                    #RUNRESPONSE
                    elif catch_response is 3:
                        catch_pid = 'ran'
                        log.warning('***CATCHING DUDES*** Pokemon ran!')
                    #FAILEDCATCHRESPONSE
                    elif catch_response is 2:
                        log.warning('Current pokeball is %s, Ball Count is %s', current_ball, pokeball_count)
                        #ADJUSTBALLCOUNT
                        if current_ball == 1:
                            pokeball_count = pokeball_count - 1
                        elif current_ball == 2:
                            greatball_count = greatball_count - 1
                        else:
                            ultraball_count = ultraball_count - 1
                        #BALLUPPER
                        if ultraball_count > 0:
                            current_ball = 3
                        elif ultraball_count == 0 and greatball_count > 0:
                            current_ball = 2
                        elif ultraball_count == 0 and greatball_count == 0 and pokeball_count > 0:
                            current_ball = 1
                        else:
                            log.warning('***CATCHING DUDES*** Out of pokeballs!')
                            catch_pid = 'blueballs'
                        log.warning('***CATCHING DUDES*** Catch failed, balling up if possible, new pokeball is %s', current_ball)
                    else:
                        continue

            if catch_pid is 'ran':
                log.warning('***CATCHING DUDES***Pokemon Ran')
            if catch_pid is 'blueballs':
                log.warning('***CATCHING DUDES***Get more pokeballs! Need to collect more.')
            else:
                # check inventory again and see if ditto - wait a second first to avoid throttling
                time.sleep(10)  # lol
                req = api.create_request()
                new_inv_get = req.check_challenge()
                new_inv_get = req.get_hatched_eggs()
                new_inv_get = req.get_inventory()
                new_inv_get = req.check_awarded_badges()
                new_inv_get = req.download_settings()
                new_inv_get = req.get_buddy_walked()
                new_inv_get = req.call()
                # https://github.com/norecha/PokeInventory/blob/master/inventory.py
                inventory = new_inv_get['responses']['GET_INVENTORY']['inventory_delta']['inventory_items']
                # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                captcha_url = new_inv_get['responses']['CHECK_CHALLENGE']['challenge_url']
                if len(captcha_url) > 1:
                    log.warning('fuck, captcha\'d, **DURING DITTO CHECK** now return Bad Scan so that this can be re-scanned')
                    return {
                        'count': 0,
                        'gyms': gyms,
                        'bad_scan': True,
                        'captcha': True,
                        'failed': 'catching'
                    }

                for item in inventory:
                    inventory_item_data = item['inventory_item_data']

                    if not inventory_item_data:
                        continue

                    if 'pokemon_data' in inventory_item_data:
                        pokemonItem = inventory_item_data['pokemon_data']
                        #    log.warning('***CATCHING DUDES***dump-PID%s:%s, the pokemon[\'id\'] is %s', index, pokemon['pokemon_id'], pokemon['id'])

                        if 'is_egg' in pokemonItem and pokemonItem['is_egg']:
                            continue

                        if pokemonItem['id'] == catch_pid:
                            # this pokemonItem is the most recent caught - is it ditto-mon
                            if pokemonItem['pokemon_id'] == 132:
                                log.warning('******CATCHING DUDES****** +++++++++++++DITTOFOUND+++++++++++++++++++++++++++++++++++++++++++++++++++++')
                                #pokemons[p['encounter_id']]['previous_id'] = p['pokemon_data']['pokemon_id']
                                pokemons[p['encounter_id']]['pokemon_id'] = 132 # Ditto Webhook And DB
                                pokemons[encounter_id].update({
                                    'previous_id': p['pokemon_data']['pokemon_id'],
                                    #'pokemon_id': '132' # Updates DB But No Webhook
                                })
                                #log.exception(previous_id)
                                # keep it dittos are lit
                                #break

                                #pokemons[encounter_id].update({
                                #    'previous_id': p['pokemon_data']['pokemon_id']
                                #})
                                #previous_id = p['pokemon_data']['pokemon_id']
                                # destroy it
                            else:
                                log.warning('***CATCHING DUDES***It\'s not a ditto')

                            release_get_result = 0  # lol
                            while release_get_result != 1:
                                time.sleep(10)  # lol
                                req = api.create_request()
                                release_get = req.check_challenge()
                                release_get = req.get_hatched_eggs()
                                release_get = req.get_inventory()
                                release_get = req.check_awarded_badges()
                                release_get = req.download_settings()
                                release_get = req.get_buddy_walked()
                                release_get = req.release_pokemon(pokemon_id=pokemonItem['id'])
                                release_get = req.call()
                                release_get_result = release_get['responses']['RELEASE_POKEMON']['result']
                                # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                                captcha_url = release_get['responses']['CHECK_CHALLENGE']['challenge_url']
                                if len(captcha_url) > 1:
                                    log.warning('fuck, captcha\'d, **DURING DITTO CHECK** now return Bad Scan so that this can be re-scanned')
                                    return {
                                        'count': 0,
                                        'gyms': gyms,
                                        'bad_scan': True,
                                        'captcha': True,
                                        'failed': 'catching'
                                    }
                            if release_get_result == 1:
                                log.warning('***CATCHING DUDES***Pokemon disposed')
                                break
                            else:
                                log.exception('***CATCHING DUDES*** disposing failed - trying again in 10 sec')

    else:
        if encounter_result is not None:
            log.warning("Error encountering {}, status code: {}".format(encounter_id, encounter_result['responses']['ENCOUNTER']['status']))
        pokemons[encounter_id].update({
            'individual_attack': None,
            'individual_defense': None,
            'individual_stamina': None,
            'move_1': None,
            'move_2': None,
            'height': None,
            'weight': None,
            'gender': None,
            'form': None,
            'previous_id': None,
            #'cp': None,
            #'pokemon_level': None,
            #'trainer_level': None,
        })

def get_player_level(map_dict):
    inventory_items = map_dict['responses'].get(
        'GET_INVENTORY', {}).get(
        'inventory_delta', {}).get(
        'inventory_items', [])
    player_stats = [item['inventory_item_data']['player_stats']
                    for item in inventory_items
                    if 'player_stats' in item.get(
                    'inventory_item_data', {})]
    if len(player_stats) > 0:
        player_level = player_stats[0].get('level', 1)
        return player_level

    return 0

def calc_pokemon_level(pokemon_info):
    cpm = pokemon_info["cp_multiplier"]
    if cpm < 0.734:
        level = 58.35178527 * cpm * cpm - 2.838007664 * cpm + 0.8539209906
    else:
        level = 171.0112688 * cpm - 95.20425243
    level = (round(level) * 2) / 2.0
    return level

# todo: this probably shouldn't _really_ be in "models" anymore, but w/e
def parse_map(args, map_dict, step_location, db_update_queue, wh_update_queue, api, status, account):
    pokemons = {}
    pokestops = {}
    gyms = {}
    skipped = 0
    stopsskipped = 0
    forts = None
    lure_info = None
    lure_expiration = None
    active_fort_modifier = None
    wild_pokemon = None
    pokesfound = False
    nearbyfound = False
    fortsfound = False
    alreadyLeveled = False
    level = 0
    USELESS = [101, 102, 103, 104, 201, 202, 701, 702, 703, 704, 705]
    forbidden = False
    totalDisks = 0
    encountered_pokemon = []
    fort_pokemon = [] 

    cells = map_dict['responses']['GET_MAP_OBJECTS']['map_cells']
    for cell in cells:
        if len(cell.get('nearby_pokemons', [])) > 0:
            nearbyfound = True
        if config['parse_pokemon']:
            if len(cell.get('wild_pokemons', [])) > 0:
                pokesfound = True
                if wild_pokemon is None:
                    wild_pokemon = cell.get('wild_pokemons', [])
                else:
                    wild_pokemon += cell.get('wild_pokemons', [])

        if config['parse_pokestops'] or config['parse_gyms']:
            if len(cell.get('forts', [])) > 0:
                fortsfound = True
                if forts is None:
                    forts = cell.get('forts', [])
                else:
                    forts += cell.get('forts', [])

    for items in map_dict['responses']['GET_INVENTORY']['inventory_delta']['inventory_items']:
        inventory_item_data = items['inventory_item_data']
        if 'player_stats' in inventory_item_data:
            level = inventory_item_data['player_stats']['level']
            currentExp = inventory_item_data['player_stats']['experience']
            nextLevel = inventory_item_data['player_stats']['next_level_xp']
            if int(level) < int(args.level_cap):
                log.warning('ACCOUNT: %s $$$$$$$$PLAYERSTATS$$$$$$$$ [Level %s] ', format(account['username']), level)

        if args.ditto is True and int(level) < int(args.level_cap) and 'item' in inventory_item_data and inventory_item_data['item']['item_id'] == 1:
            pokeball_count = inventory_item_data['item'].get('count', 0)
            log.warning('ACCOUNT: %s @@@@@@@@INVENTORY@@@@@@@@ [%s regular] pokeballs', format(account['username']), pokeball_count)
        if args.ditto is True and int(level) < int(args.level_cap) and 'item' in inventory_item_data and inventory_item_data['item']['item_id'] == 2:
            greatball_count = inventory_item_data['item'].get('count', 0)
            log.warning('ACCOUNT: %s @@@@@@@@INVENTORY@@@@@@@@ [%s regular] pokeballs', format(account['username']), greatball_count)
        if args.ditto is True and int(level) < int(args.level_cap) and 'item' in inventory_item_data and inventory_item_data['item']['item_id'] == 3:
            ultraball_count = inventory_item_data['item'].get('count', 0)
            log.warning('ACCOUNT: %s @@@@@@@@INVENTORY@@@@@@@@ [%s regular] pokeballs', format(account['username']), ultraball_count)

        if args.doPstop is True and int(level) < int(args.level_cap) and 'item' in inventory_item_data and inventory_item_data['item']['item_id'] == 501:
            totalDisks = inventory_item_data['item'].get('count', 0)
            log.warning('ACCOUNT: %s @@@@@@@@INVENTORY@@@@@@@@ [%s lure] Modules', format(account['username']), totalDisks)

    if pokesfound:
        encounter_ids = [b64encode(str(p['encounter_id'])) for p in wild_pokemon]
        # For all the wild pokemon we found check if an active pokemon is in the database
        query = (Pokemon
                 .select(Pokemon.encounter_id, Pokemon.spawnpoint_id)
                 .where((Pokemon.disappear_time > datetime.utcnow()) & (Pokemon.encounter_id << encounter_ids))
                 .dicts())

        # Store all encounter_ids and spawnpoint_id for the pokemon in query (all thats needed to make sure its unique)
        encountered_pokemon = [(p['encounter_id'], p['spawnpoint_id']) for p in query]

        for p in wild_pokemon:
            if (b64encode(str(p['encounter_id'])), p['spawn_point_id']) in encountered_pokemon:
                # This pokemon has been encountered before, let's check if the new one has valid time. If not, skip.
                if 0 < p['time_till_hidden_ms'] < 3600000:
                    Pokemon.delete().where(Pokemon.encounter_id == b64encode(str(p['encounter_id']))).execute()
                else:
                    # No valid time. Skip.
                    skipped += 1
                    continue

            time_detail = -1

            # time_till_hidden_ms was overflowing causing a negative integer.
            # It was also returning a value above 3.6M ms.
            if 0 < p['time_till_hidden_ms'] < 3600000:
                time_detail = 1
                d_t = datetime.utcfromtimestamp(
                    (p['last_modified_timestamp_ms'] +
                     p['time_till_hidden_ms']) / 1000.0)
            else:
                # Set a value of 30 minutes because currently its unknown but larger than 30.
                predicted_time = Pokemon.predict_disappear_time(p['spawn_point_id'])
                if not isinstance(predicted_time, datetime):
                    d_t = datetime.utcfromtimestamp((p['last_modified_timestamp_ms'] + 1800000) / 1000.0)
                else:
                    d_t = predicted_time
                    time_detail = 0

            printPokemon(p['pokemon_data']['pokemon_id'], p['latitude'], p['longitude'], d_t)

            # Scan for IVs and moves
            encounter_result = None
            if (args.encounter and (p['pokemon_data']['pokemon_id'] in args.encounter_whitelist or p['pokemon_data']['pokemon_id'] not in args.encounter_blacklist and not args.encounter_whitelist)):
                time.sleep(args.encounter_delay)
                # Set up encounter request envelope
                req = api.create_request()
                encounter_result = req.encounter(encounter_id=p['encounter_id'],
                                                 spawn_point_id=p['spawn_point_id'],
                                                 player_latitude=step_location[0],
                                                 player_longitude=step_location[1])
                encounter_result = req.check_challenge()
                encounter_result = req.get_hatched_eggs()
                encounter_result = req.get_inventory()
                encounter_result = req.check_awarded_badges()
                encounter_result = req.download_settings()
                encounter_result = req.get_buddy_walked()
                encounter_result = req.call()

            construct_pokemon_dict(pokemons, p, encounter_result, d_t, api, nextLevel, currentExp, level, time_detail)
            #log.warning('++++++++++++++++++++++++++++++++++POKEMON: %s++++++++++++++++++++++++++++++++++', pokemons)
            #log.warning('IS IT REG ENCOUNTERS? %s', encounter_result)

            #CONSTRUCT WEBHOOK POKEMON
            if pokemons[p['encounter_id']]['pokemon_id'] is not 132:
                whpokemon = p['pokemon_data']['pokemon_id']
            elif pokemons[p['encounter_id']]['pokemon_id'] is 132:
                whpokemon = pokemons[p['encounter_id']]['pokemon_id']

            if args.webhooks:
                wh_update_queue.put(('pokemon', {
                    'encounter_id': b64encode(str(p['encounter_id'])),
                    'spawnpoint_id': p['spawn_point_id'],
                    'pokemon_id': whpokemon,
                    'latitude': p['latitude'],
                    'longitude': p['longitude'],
                    'disappear_time': calendar.timegm(d_t.timetuple()),
                    'last_modified_time': p['last_modified_timestamp_ms'],
                    'time_until_hidden_ms': p['time_till_hidden_ms'],
                    'individual_attack': pokemons[p['encounter_id']]['individual_attack'],
                    'individual_defense': pokemons[p['encounter_id']]['individual_defense'],
                    'individual_stamina': pokemons[p['encounter_id']]['individual_stamina'],
                    'move_1': pokemons[p['encounter_id']]['move_1'],
                    'move_2': pokemons[p['encounter_id']]['move_2'],
                    'time_detail': time_detail,
                    'height': pokemons[p['encounter_id']]['height'],
                    'weight': pokemons[p['encounter_id']]['weight'],
                    'gender': pokemons[p['encounter_id']]['gender'],
                    'form': pokemons[p['encounter_id']]['form'],
                    'previous_id': pokemons[p['encounter_id']]['previous_id'],
 
                }))
                #log.warning('++++++++++++++++++++++++++++++++++pokemon_id: %s++++++++++++++++++++++++++++++++++', whpokemon)
                #log.warning('++++++++++++++++++++++++++++++++++previous_id: %s++++++++++++++++++++++++++++++++++', pokemons[p['encounter_id']]['previous_id'])

    if fortsfound:
        if config['parse_pokestops']:
            stop_ids = [f['id'] for f in forts if f.get('type') == 1]
            if len(stop_ids) > 0:
                query = (Pokestop
                         .select(Pokestop.pokestop_id, Pokestop.last_modified)
                         .where((Pokestop.pokestop_id << stop_ids))
                         .dicts())
                encountered_pokestops = [(f['pokestop_id'], int((f['last_modified'] - datetime(1970, 1, 1)).total_seconds())) for f in query]
        for f in forts:
            if config['parse_pokestops'] and f.get('type') == 1:  # Pokestops
                
                distance = 0.03
                egg = None
                bater = None
                breakableId = None
                unbreakableId = None
                monID = None
                monCount = 0
                usedIncubatorCount = 0
                #totalDisks = 0
                if args.doPstop is True and in_radius((f['latitude'], f['longitude']), step_location, distance):
                    spin_result = None
                    req = api.create_request()
                    log.warning('Pokestop ID: %s', f['id'])
                    while spin_result is None:
                        spin_response = req.fort_search(fort_id=f['id'],
                                                        fort_latitude=f['latitude'],
                                                        fort_longitude=f['longitude'],
                                                        player_latitude=step_location[0],
                                                        player_longitude=step_location[1]
                                                        )
                        spin_response = req.check_challenge()
                        spin_response = req.get_hatched_eggs()
                        spin_response = req.get_inventory()
                        spin_response = req.check_awarded_badges()
                        spin_response = req.download_settings()
                        spin_response = req.get_buddy_walked()
                        time.sleep(10)
                        spin_response = req.call()
                        # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                        captcha_url = spin_response['responses']['CHECK_CHALLENGE']['challenge_url']
                        if len(captcha_url) > 1:
                            log.warning('fuck, captcha\'d, now return Bad Scan so that this can be re-scanned')
                            return {
                                'count': 0,
                                'gyms': gyms,
                                'bad_scan': True,
                                'captcha': True,
                                'failed': 'stopspinning'
                            }
                        if spin_response['responses']['FORT_SEARCH']['result'] is 1:
                            log.warning('&&&SPINNING&&& Spin stop Account %s success', format(account['username']))
                            spin_result = 1
                            awardedExp = spin_response['responses']['FORT_SEARCH']['experience_awarded']
                            #log.warning('$$$PLAYERSTATS$$$ xp is : %s', awardedExp)
                            oldExp = currentExp
                            currentExp = currentExp + awardedExp
                            log.warning('$$$PLAYERSTATS$$$ Spun pokestop so increased XP by %s, old XP was, %s now is %s, next level at %s, Account %s', awardedExp, oldExp, currentExp, nextLevel, format(account['username']))
                            if currentExp == nextLevel or currentExp > nextLevel:
                                log.warning('$$$PLAYERSTATS$$$ LEVEL UP DETECTED OH SHIT. Account %s', format(account['username']))
                                levelup = level + 1
                                levelStatus = None
                                while levelStatus is None:
                                    if alreadyLeveled is True:
                                        log.warning('$$$PLAYERSTATS$$$ But actually we already leveled up fam. Nvm.')
                                        break
                                    req = api.create_request()
                                    levelResponse = req.level_up_rewards(level=levelup)
                                    time.sleep(1)
                                    levelResponse = req.call()
                                    if levelResponse['responses']['LEVEL_UP_REWARDS']['result']:
                                        levelStatus = levelResponse['responses']['LEVEL_UP_REWARDS']['result']
                                        if levelStatus == 0:
                                            log.exception('$$$PLAYERSTATS$$$ SHIT IT\'S UNSET WHAT DOES THAT MEAN')
                                        elif levelStatus == 1:
                                            log.warning('$$$PLAYERSTATS$$$ Level up SUCC CESS - Account %s Now level %s', format(account['username']), levelup)
                                        elif levelStatus == 2:
                                            log.exception('$$$PLAYERSTATS$$$ Level up reward ALREADY TAKEN the code is BROKE')
                                        else:
                                            log.exception('$$$PLAYERSTATS$$$ UNKNOWN, SHIT IS BLANK')
                                    else:
                                        log.exception('$$$PLAYERSTATS$$$ LEVEL UP FAILED! Level up has already been done')
                                        levelStatus = 0
                        elif spin_response['responses']['FORT_SEARCH']['result'] is 2:
                            log.exception('&&&SPINNING&&& Stop is out of range - this formula needs fixing. Account %s', format(account['username']))
                            spin_result = 'Failed'
                        elif spin_response['responses']['FORT_SEARCH']['result'] is 3:
                            log.warning('&&&SPINNING&&& Already spun this stop - check for this one day. Account %s', format(account['username']))
                            spin_result = 'Failed'
                        elif spin_response['responses']['FORT_SEARCH']['result'] is 4:
                            log.exception('&&&SPINNING&&& Inventory is full (idk how you managed this one). Account %s', format(account['username']))
                            spin_result = 'Failed'
                        elif spin_response['responses']['FORT_SEARCH']['result'] is 5:
                            log.exception('&&&SPINNING&&& Maximum spun stops for the day - idk how you managed this either. Account %s', format(account['username']))
                            spin_result = 'Failed'
                        else:
                            log.exception('&&&SPINNING&&&No result set - weird error - abort mission. Account %s', format(account['username']))
                            spin_result = 'Failed'
                        inventory = spin_response['responses']['GET_INVENTORY']['inventory_delta']['inventory_items']
                        for item in inventory:
                            inventory_item_data = item['inventory_item_data']
                            if not inventory_item_data:
                                continue
                            if 'pokemon_data' in inventory_item_data:
                                pokememe = inventory_item_data['pokemon_data']
                                if 'is_egg' in pokememe and pokememe['is_egg'] and 'egg_incubator_id' not in pokememe:
                                    #log.warning('###EGGS### FOUND AN EGG %s Account %s', pokememe['id'], format(account['username']))
                                    egg = pokememe['id']
                                else:
                                    #log.warning('###MONS### FOUND A MON %s Account: %s', pokememe['id'], format(account['username']))
                                    monCount += 1
                                    monID = pokememe['id']
                            if args.doEgg is True and 'egg_incubators' in inventory_item_data and int(level) < int(args.level_cap):
                                incubators = inventory_item_data['egg_incubators']
                                count = -1
                                for incubator in incubators:
                                    itemid = inventory_item_data['egg_incubators']['egg_incubator'][count]['item_id']
                                    count += 1
                                    #log.warning('###EGGS### THE ITEM ID %s', itemid)
                                    if 'pokemon_id' in inventory_item_data['egg_incubators']['egg_incubator'][count]:
                                        log.warning('###EGGS### FOUND A USED BATOR %s', itemid)
                                        usedIncubatorCount += 1
                                    else:
                                        if itemid == 901:
                                            unbreakableId = inventory_item_data['egg_incubators']['egg_incubator'][count]['id']
                                            log.warning('###EGGS### HAVE A BATOR GONNA BATE IT: %s Account: %s', unbreakableId, format(account['username']))
                                        else:
                                            breakableId = inventory_item_data['egg_incubators']['egg_incubator'][count]['id']
                                            log.warning('###EGGS### HAVE A breakorRRR GONNA breaGk IT: %s Account: %s', breakableId, format(account['username']))

                            #TRASH USELESSITEMS
                            if 'item' in inventory_item_data and inventory_item_data['item']['item_id'] in USELESS:
                                totalItems = inventory_item_data['item'].get('count', 0)
                                trashingItems = totalItems - 2 # Keep 3 Items Only
                                if inventory_item_data['item'].get('count', 0) > 10: # If Items are Above 10
                                    log.warning('@@@INVENTORY@@@ too many potions/berries Trashing: %s', trashingItems)
                                    trash_status = None
                                    while trash_status is None:
                                        req = api.create_request()
                                        trash_result = req.check_challenge()
                                        trash_result = req.get_hatched_eggs()
                                        trash_result = req.get_inventory()
                                        trash_result = req.check_awarded_badges()
                                        trash_result = req.download_settings()
                                        trash_result = req.get_buddy_walked()
                                        trash_result = req.recycle_inventory_item(item_id=inventory_item_data['item']['item_id'],
                                                                                  count=trashingItems)
                                        time.sleep(4.20)
                                        trash_result = req.call()
                                        # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                                        captcha_url = trash_result['responses']['CHECK_CHALLENGE']['challenge_url']
                                        if len(captcha_url) > 1:
                                            log.warning('fuck, captcha\'d, **DURING ITEM TRASHING** now return Bad Scan so that this can be re-scanned')
                                            return {
                                                'count': 0,
                                                'gyms': gyms,
                                                'bad_scan': True,
                                                'captcha': True,
                                                'failed': 'trashing'
                                            }
                                        # log.warning('@@@INVENTORY@@@ %s remaining of ID: %s', trash_result['responses']['RECYCLE_INVENTORY_ITEM']['new_count'], inventory_item_data['item']['item_id'])
                                        if trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 1:
                                            log.warning('@@@INVENTORY@@@ recycle success')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 2:
                                            log.exception('@@@INVENTORY@@@ not enough items to trash - parsing messed up')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 3:
                                            trash_status = 1
                                            log.exception('@@@INVENTORY@@@ tried to recycle incubator - parsing messed up again')
                                        else:
                                            log.exception('trashing failed - panic')
                                            trash_status = 1
                            #TRASH Regular BALLS
                            if 'item' in inventory_item_data and inventory_item_data['item']['item_id'] is 1:
                                if inventory_item_data['item'].get('count', 0) > 200:
                                    log.warning('@@@INVENTORY@@@ too many regular balls, recyling.....')
                                    trash_status = None
                                    while trash_status is None:
                                        req = api.create_request()
                                        trash_result = req.check_challenge()
                                        trash_result = req.get_hatched_eggs()
                                        trash_result = req.get_inventory()
                                        trash_result = req.check_awarded_badges()
                                        trash_result = req.download_settings()
                                        trash_result = req.get_buddy_walked()
                                        trash_result = req.recycle_inventory_item(item_id=inventory_item_data['item']['item_id'],
                                                                                  count=50)
                                        time.sleep(4.20)
                                        trash_result = req.call()
                                        # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                                        captcha_url = trash_result['responses']['CHECK_CHALLENGE']['challenge_url']
                                        if len(captcha_url) > 1:
                                            log.warning('fuck, captcha\'d, **DURING ITEM TRASHING** now return Bad Scan so that this can be re-scanned')
                                            return {
                                                'count': 0,
                                                'gyms': gyms,
                                                'bad_scan': True,
                                                'captcha': True,
                                                'failed': 'trashing'
                                            }
                                        # log.warning('@@@INVENTORY@@@ %s remaining of ID: %s', trash_result['responses']['RECYCLE_INVENTORY_ITEM']['new_count'], inventory_item_data['item']['item_id'])
                                        if trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 1:
                                            log.warning('@@@INVENTORY@@@ recycle success')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 2:
                                            log.exception('@@@INVENTORY@@@ not enough items to trash - parsing messed up')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 3:
                                            trash_status = 1
                                            log.exception('@@@INVENTORY@@@ tried to recycle incubator - parsing messed up again')
                                        else:
                                            log.exception('trashing failed - panic')
                                            trash_status = 1
                            #TRASH Great BALLS
                            if 'item' in inventory_item_data and inventory_item_data['item']['item_id'] is 2:
                                if inventory_item_data['item'].get('count', 0) > 50:
                                    log.warning('@@@INVENTORY@@@ too many great balls, recyling.....')
                                    trash_status = None
                                    while trash_status is None:
                                        req = api.create_request()
                                        trash_result = req.check_challenge()
                                        trash_result = req.get_hatched_eggs()
                                        trash_result = req.get_inventory()
                                        trash_result = req.check_awarded_badges()
                                        trash_result = req.download_settings()
                                        trash_result = req.get_buddy_walked()
                                        trash_result = req.recycle_inventory_item(item_id=inventory_item_data['item']['item_id'],
                                                                                  count=20)
                                        time.sleep(4.20)
                                        trash_result = req.call()
                                        # REMEMBER TO CHECK FOR CAPTCHAS WITH EVERY REQUEST
                                        captcha_url = trash_result['responses']['CHECK_CHALLENGE']['challenge_url']
                                        if len(captcha_url) > 1:
                                            log.warning('fuck, captcha\'d, **DURING ITEM TRASHING** now return Bad Scan so that this can be re-scanned')
                                            return {
                                                'count': 0,
                                                'gyms': gyms,
                                                'bad_scan': True,
                                                'captcha': True,
                                                'failed': 'trashing'
                                            }
                                        # log.warning('@@@INVENTORY@@@ %s remaining of ID: %s', trash_result['responses']['RECYCLE_INVENTORY_ITEM']['new_count'], inventory_item_data['item']['item_id'])
                                        if trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 1:
                                            log.warning('@@@INVENTORY@@@ recycle success')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 2:
                                            log.exception('@@@INVENTORY@@@ not enough items to trash - parsing messed up')
                                            trash_status = 1
                                        elif trash_result['responses']['RECYCLE_INVENTORY_ITEM']['result'] is 3:
                                            trash_status = 1
                                            log.exception('@@@INVENTORY@@@ tried to recycle incubator - parsing messed up again')
                                        else:
                                            log.exception('trashing failed - panic')
                                            trash_status = 1

                        if breakableId is not None and egg is not None or unbreakableId is not None and egg is not None:
                            if breakableId is None:
                                bater = unbreakableId
                            else:
                                bater = breakableId

                            egg_status = None
                            while egg_status is None:
                                req = api.create_request()
                                egg_request = req.use_item_egg_incubator(item_id=bater,
                                                                         pokemon_id=egg)
                                time.sleep(4.20)
                                egg_request = req.call()
                                egg_status = egg_request['responses']['USE_ITEM_EGG_INCUBATOR']['result']
                                if egg_status is 0:
                                    log.exception('###EGG### Server responded with "unset" - what the fukc')
                                elif egg_status is 1:
                                    log.warning('###EGG### Egg incubation success - egg set')
                                    breakableId = None
                                    unbreakableId = None
                                    break
                                elif egg_status is 2:
                                    log.exception('###EGG### Incubator not found! Parsing issues with above!')
                                elif egg_status is 3:
                                    log.exception('###EGG### Egg not found! Parsing issues with above! Egg: %s', egg)
                                elif egg_status is 4:
                                    log.exception('###EGG### Given ID does not point to EGG! Parsing issues!')
                                elif egg_status is 5:
                                    log.exception('###EGG### Incubator in use! Still Parsing issues!!')
                                elif egg_status is 6:
                                    log.exception('###EGG### Egg already incubating! These parsing Issues!!')
                                elif egg_status is 7:
                                    log.exception('###EGG### This incubator is broken! Somehow used old inventory? PARSING')
                        if monCount > 3:
                            release_status = None
                            while release_status is None:
                                req = api.create_request()
                                release_request = req.release_pokemon(pokemon_id=monID)
                                time.sleep(4.20)
                                release_request = req.call()
                                release_status = release_request['responses']['RELEASE_POKEMON']['result']
                                if release_status == 1:
                                    log.warning('ACCOUNT: %s ########MONS######## Excess pokemon removed, %s pokemon remaining', format(account['username']), monCount)
                                    break
                                else:
                                    log.exception('ACCOUNT: %s ########MONS######## Excess pokemon removal failed - trying again in 10 sec', format(account['username']))
                
                if 'active_fort_modifier' in f:
                    lure_info = f.get('lure_info')

                    # Lure Master Code
                    if luremaster is not None:
                        luremaster = luremaster
                    else:
                        luremaster = None
                    #Player Lure Info Request
                    req = api.create_request()
                    req.fort_details(fort_id=f['id'],
                                     latitude=f['latitude'],
                                     longitude=f['longitude'])
                    time.sleep(4.20)
                    fort_details_request = req.call()
                    #log.warning(fort_details_request)
                    try:
                        luremaster = fort_details_request['responses']['FORT_DETAILS']['modifiers'][0]['deployer_player_codename']
                    except Exception:
                        log.warning('Initial get-inventory failed!!')
                    log.warning('$$$LUREMASTER IS : %s', luremaster)

                    lure_expiration = datetime.utcfromtimestamp(
                        f['last_modified_timestamp_ms'] / 1000.0) + timedelta(minutes=args.lure_duration)   # timedelta(minutes=30)
                    active_fort_modifier = f['active_fort_modifier']

                    if args.webhooks and args.webhook_updates_only:
                        wh_update_queue.put(('pokestop', {
                            'pokestop_id': b64encode(str(f['id'])),
                            'enabled': f['enabled'],
                            'latitude': f['latitude'],
                            'longitude': f['longitude'],
                            'last_modified_time': f['last_modified_timestamp_ms'],
                            'lure_expiration': calendar.timegm(lure_expiration.timetuple()),
                            'active_fort_modifier': active_fort_modifier
                        }))

                    if args.lured_pokemon and lure_info is not None and config['parse_pokemon']:
                        # pre-build a list of encountered pokemon
                        fort_encounter_id = [b64encode(str(lure_info['encounter_id']))]
                        if fort_encounter_id:
                            query = (Pokemon
                                     .select()
                                     .where((Pokemon.disappear_time > datetime.utcnow()) & (Pokemon.encounter_id << fort_encounter_id))
                                     .dicts()
                                     )
                            fort_pokemon = [(p['encounter_id'], p['pokestop_id']) for p in query]

                        # Don't parse pokemon we've already encountered. Avoids IVs getting nulled out on rescanning.
                        if (b64encode(str(lure_info['encounter_id'])), f['id']) in fort_pokemon:
                            skipped += 1
                            continue

                        d_t = datetime.utcfromtimestamp(lure_info['lure_expires_timestamp_ms'] / 1000)

                        encounter_result = None
                        if (args.encounter and (lure_info['active_pokemon_id'] in args.encounter_whitelist or
                                                lure_info['active_pokemon_id'] not in args.encounter_blacklist and not args.encounter_whitelist)):
                            time.sleep(args.encounter_delay)
                            #encounter_result = api.disk_encounter(encounter_id=lure_info['encounter_id'],
                            #                                      fort_id=f['id'],
                            #                                      player_latitude=step_location[0],
                            #                                      player_longitude=step_location[1])
                            req = api.create_request()
                            encounter_result = req.disk_encounter(encounter_id=lure_info['encounter_id'],
                                                                  fort_id=f['id'],
                                                                  player_latitude=step_location[0],
                                                                  player_longitude=step_location[1])
                            encounter_result = req.check_challenge()
                            encounter_result = req.get_hatched_eggs()
                            encounter_result = req.get_inventory()
                            encounter_result = req.check_awarded_badges()
                            encounter_result = req.download_settings()
                            encounter_result = req.get_buddy_walked()      
                            encounter_result = req.call() 

                        #try:
                        #    log.warning('IS IT RESPONSES? %s', encounter_result['responses'])
                        #except KeyError:
                        #    log.warning('RESPONSES DOESNT EXIST')

                        #try:
                        #    log.warning('IS IT DISK_ENCOUNTER? %s', encounter_result['responses']['DISK_ENCOUNTER'])
                        #except KeyError:
                        #    log.warning('DISK_ENCOUNTER DOESNT EXIST') 

                        construct_pokemon_dict(pokemons, f, encounter_result, d_t, api, nextLevel, currentExp, level, time_detail=1, lure_info=lure_info)


                        if args.webhooks:
                            wh_update_queue.put(('pokemon', {
                                'encounter_id': b64encode(str(lure_info['encounter_id'])),
                                'pokestop_id': b64encode(str(f['id'])),
                                'pokemon_id': lure_info['active_pokemon_id'],
                                'latitude': f['latitude'],
                                'longitude': f['longitude'],
                                'disappear_time': calendar.timegm(d_t.timetuple()),
                                'individual_attack': pokemons[lure_info['encounter_id']]['individual_attack'],
                                'individual_defense': pokemons[lure_info['encounter_id']]['individual_defense'],
                                'individual_stamina': pokemons[lure_info['encounter_id']]['individual_stamina'],
                                'move_1': pokemons[lure_info['encounter_id']]['move_1'],
                                'move_2': pokemons[lure_info['encounter_id']]['move_2'],
                                #'previous_id': previous_id,
                            }))
                else:

                    distance = 0.03
                    if in_radius((f['latitude'], f['longitude']), step_location, distance):
                        if args.setLure is True:
                            if args.lureFence is not None:
                                allowed = geofence(step_location, args.lureFence)
                                log.warning('FENCE: %s', allowed)
                                if allowed == []:
                                    log.warning('STOP IS FORBIDDEN')
                                    forbidden = True
                                else:
                                    log.warning('STOP IS GOOD')
                                    forbidden = False
                            if args.nolureFence is not None:
                                forbidden = geofence(step_location, args.nolureFence, forbidden=True)
                                log.warning('DI-ALLOWFENCE: %s', forbidden)
                                if forbidden == []:
                                    log.warning('STOP IS GOOD')
                                    forbidden = False
                                else:
                                    forbidden = True
                                    log.warning('STOP IS FORBIDDEN')
                            
                            lure_status = None
                            lure_id = 501
                            if totalDisks == 0:
                                #log.warning('DETECTING %s LURES', totalDisks)
                                forbidden = True
                            while lure_status is None and totalDisks > 0 and forbidden is False:
                                req = api.create_request()
                                lure_request = req.add_fort_modifier(modifier_type=lure_id,
                                                                     fort_id=f['id'],
                                                                     player_latitude=step_location[0],
                                                                     player_longitude=step_location[1])
                                time.sleep(4.20)
                                lure_request = req.call()
                                #log.warning('@@@LURE RESPONSE@@@ %s', lure_request['responses'])
                                lure_status = lure_request['responses']['ADD_FORT_MODIFIER']['result']
                                if lure_status is 0:
                                    log.warning('ACCOUNT: %s ███Lure was unset! Shiet son███', format(account['username']))
                                    lure_status = 'Failed'
                                elif lure_status is 1:
                                    log.warning('ACCOUNT: %s ███Lure successfully set! holy SHEIT███', format(account['username']))
                                    lure_status = 'Win'
                                elif lure_status is 2:
                                    log.warning('ACCOUNT: %s ███Stop already has lure!!███', format(account['username']))
                                    lure_status = 'Panic'
                                elif lure_status is 3:
                                    log.warning('ACCOUNT: %s ███Out of range to set lure! (how?)███', format(account['username']))
                                    lure_status = 'Range'
                                elif lure_status is 4:
                                    log.warning('ACCOUNT: %s ███Account has no lures!███', format(account['username']))
                                    lure_status = 'empty'

                    lure_expiration, active_fort_modifier, luremaster = None, None, None

                # Send all pokéstops to webhooks
                if args.webhooks and not args.webhook_updates_only:
                    # Explicitly set 'webhook_data', in case we want to change the information pushed to webhooks,
                    # similar to above and previous commits.
                    l_e = None

                    if lure_expiration is not None:
                        l_e = calendar.timegm(lure_expiration.timetuple())

                    wh_update_queue.put(('pokestop', {
                        'pokestop_id': b64encode(str(f['id'])),
                        'enabled': f['enabled'],
                        'latitude': f['latitude'],
                        'longitude': f['longitude'],
                        'last_modified': f['last_modified_timestamp_ms'],
                        'lure_expiration': l_e,
                        'active_fort_modifier': active_fort_modifier
                    }))

                if (f['id'], int(f['last_modified_timestamp_ms'] / 1000.0)) in encountered_pokestops:
                    # If pokestop has been encountered before and hasn't changed dont process it.
                    stopsskipped += 1
                    continue

                pokestops[f['id']] = {
                    'pokestop_id': f['id'],
                    'enabled': f['enabled'],
                    'latitude': f['latitude'],
                    'longitude': f['longitude'],
                    'last_modified': datetime.utcfromtimestamp(f['last_modified_timestamp_ms'] / 1000.0),
                    'lure_expiration': lure_expiration,
                    'active_fort_modifier': active_fort_modifier,
                    'player_lure': luremaster
                }

            elif config['parse_gyms'] and f.get('type') is None:  # Currently, there are only stops and gyms
                #log.warning('GYM+++++++++++++++++++++++++++++++%s', f)
                prevpoints = None
                train_battle = None
                is_active = None
                distance = 0.40
                # Gym In Range
                if in_radius((f['latitude'], f['longitude']), step_location, distance):
                    # Gym In Battle
                    if 'is_in_battle' in f:
                        is_active = 1
                        log.warning('ACCOUNT: %s, THE GYM IS CURRENTLY BEING BATTLED', format(account['username']))
                        #log.warning('GYM %s IS CURRENTLY BEING BATTLED', f['id'])	

                    currentGym = f['id']
                    Query = Gym.select().where(Gym.gym_id == currentGym).dicts()
                    prevpoints = None
                    for meme in list(Query):
                        #gyms = meme['gym_id']
                        prevpoints = meme['gym_points']
                        #prevScan = meme['last_scanned']
                    #log.warning('CAAAASH MONEY %s /// %s', prevScan, (datetime.utcfromtimestamp(f['last_modified_timestamp_ms'] / 1000.0) + timedelta(minutes=1)))

                    if prevpoints <> f.get('gym_points', 0):
                        #log.warning('prevpoints IS: %s AND NEW POINTS IS: %s', prevpoints, f.get('gym_points', 0))
                        time.sleep(10)
                        if prevpoints < f.get('gym_points', 0):
                            log.warning('ACCOUNT: %s, Gym is being TRAINED', format(account['username']))
                            train_battle = 1
                        elif prevpoints > f.get('gym_points', 0):
                            log.warning('ACCOUNT: %s, Gym is being BATTLED', format(account['username']))
                            train_battle = 2
                #else:
                    #log.warning('train_battle = None')
                    #log.warning('gym not in range')

                # Send gyms to webhooks
                if args.webhooks and not args.webhook_updates_only:
                    # Explicitly set 'webhook_data', in case we want to change the information pushed to webhooks,
                    # similar to above and previous commits.
                    wh_update_queue.put(('gym', {
                        'gym_id': b64encode(str(f['id'])),
                        'team_id': f.get('owned_by_team', 0),
                        'guard_pokemon_id': f.get('guard_pokemon_id', 0),
                        'gym_points': f.get('gym_points', 0),
                        'enabled': f['enabled'],
                        'latitude': f['latitude'],
                        'longitude': f['longitude'],
                        'last_modified': f['last_modified_timestamp_ms'],
                        'is_active': is_active,
                        'train_battle': train_battle,
                    }))

                gyms[f['id']] = {
                    'gym_id': f['id'],
                    'team_id': f.get('owned_by_team', 0),
                    'guard_pokemon_id': f.get('guard_pokemon_id', 0),
                    'gym_points': f.get('gym_points', 0),
                    'enabled': f['enabled'],
                    'latitude': f['latitude'],
                    'longitude': f['longitude'],
                    'last_modified': datetime.utcfromtimestamp(f['last_modified_timestamp_ms'] / 1000.0),
                    'is_active': is_active,
                    'train_battle': train_battle,
                }

    if len(pokemons):
        db_update_queue.put((Pokemon, pokemons))
    if len(pokestops):
        db_update_queue.put((Pokestop, pokestops))
    if len(gyms):
        db_update_queue.put((Gym, gyms))

    log.info('Parsing found %d pokemons, %d pokestops, and %d gyms.',
             len(pokemons) + skipped,
             len(pokestops) + stopsskipped,
             len(gyms))

    log.debug('Skipped %d Pokemons and %d pokestops.',
              skipped,
              stopsskipped)

    db_update_queue.put((ScannedLocation, {0: {
        'latitude': step_location[0],
        'longitude': step_location[1],
        'username': status['user'],
    }}))

    return {
        'count': skipped + stopsskipped + len(pokemons) + len(pokestops) + len(gyms),
        'gyms': gyms,
        'nearby': nearbyfound,
        'neargym': fortsfound,
        'pokestops': pokestops,
    }


def parse_gyms(args, gym_responses, wh_update_queue):
    gym_details = {}
    gym_members = {}
    gym_pokemon = {}
    trainers = {}

    i = 0
    for g in gym_responses.values():
        gym_state = g['gym_state']
        gym_id = gym_state['fort_data']['id']

        gym_details[gym_id] = {
            'gym_id': gym_id,
            'name': g['name'],
            'description': g.get('description'),
            'url': g['urls'][0],
        }

        if args.webhooks:
            webhook_data = {
                'id': gym_id,
                'latitude': gym_state['fort_data']['latitude'],
                'longitude': gym_state['fort_data']['longitude'],
                'team': gym_state['fort_data'].get('owned_by_team', 0),
                'name': g['name'],
                'description': g.get('description'),
                'url': g['urls'][0],
                'pokemon': [],
            }

        for member in gym_state.get('memberships', []):
            gym_members[i] = {
                'gym_id': gym_id,
                'pokemon_uid': member['pokemon_data']['id'],
            }

            gym_pokemon[i] = {
                'pokemon_uid': member['pokemon_data']['id'],
                'pokemon_id': member['pokemon_data']['pokemon_id'],
                'cp': member['pokemon_data']['cp'],
                'trainer_name': member['trainer_public_profile']['name'],
                'num_upgrades': member['pokemon_data'].get('num_upgrades', 0),
                'move_1': member['pokemon_data'].get('move_1'),
                'move_2': member['pokemon_data'].get('move_2'),
                'height': member['pokemon_data'].get('height_m'),
                'weight': member['pokemon_data'].get('weight_kg'),
                'stamina': member['pokemon_data'].get('stamina'),
                'stamina_max': member['pokemon_data'].get('stamina_max'),
                'cp_multiplier': member['pokemon_data'].get('cp_multiplier'),
                'additional_cp_multiplier': member['pokemon_data'].get('additional_cp_multiplier', 0),
                'iv_defense': member['pokemon_data'].get('individual_defense', 0),
                'iv_stamina': member['pokemon_data'].get('individual_stamina', 0),
                'iv_attack': member['pokemon_data'].get('individual_attack', 0),
                'last_seen': datetime.utcnow(),
            }

            trainers[i] = {
                'name': member['trainer_public_profile']['name'],
                'team': gym_state['fort_data']['owned_by_team'],
                'level': member['trainer_public_profile']['level'],
                'last_seen': datetime.utcnow(),
            }

            if args.webhooks:
                webhook_data['pokemon'].append({
                    'pokemon_uid': member['pokemon_data']['id'],
                    'pokemon_id': member['pokemon_data']['pokemon_id'],
                    'cp': member['pokemon_data']['cp'],
                    'num_upgrades': member['pokemon_data'].get('num_upgrades', 0),
                    'move_1': member['pokemon_data'].get('move_1'),
                    'move_2': member['pokemon_data'].get('move_2'),
                    'height': member['pokemon_data'].get('height_m'),
                    'weight': member['pokemon_data'].get('weight_kg'),
                    'stamina': member['pokemon_data'].get('stamina'),
                    'stamina_max': member['pokemon_data'].get('stamina_max'),
                    'cp_multiplier': member['pokemon_data'].get('cp_multiplier'),
                    'additional_cp_multiplier': member['pokemon_data'].get('additional_cp_multiplier', 0),
                    'iv_defense': member['pokemon_data'].get('individual_defense', 0),
                    'iv_stamina': member['pokemon_data'].get('individual_stamina', 0),
                    'iv_attack': member['pokemon_data'].get('individual_attack', 0),
                    'trainer_name': member['trainer_public_profile']['name'],
                    'trainer_level': member['trainer_public_profile']['level'],
                })

            i += 1
        if args.webhooks:
            wh_update_queue.put(('gym_details', webhook_data))

    # All this database stuff is synchronous (not using the upsert queue) on purpose.
    # Since the search workers load the GymDetails model from the database to determine if a gym
    # needs rescanned, we need to be sure the GymDetails get fully committed to the database before moving on.
    #
    # We _could_ synchronously upsert GymDetails, then queue the other tables for
    # upsert, but that would put that Gym's overall information in a weird non-atomic state.

    # upsert all the models
    if len(gym_details):
        bulk_upsert(GymDetails, gym_details)
    if len(gym_pokemon):
        bulk_upsert(GymPokemon, gym_pokemon)
    if len(trainers):
        bulk_upsert(Trainer, trainers)

    # This needs to be completed in a transaction, because we don't wany any other thread or process
    # to mess with the GymMembers for the gyms we're updating while we're updating the bridge table.
    with flaskDb.database.transaction():
        # get rid of all the gym members, we're going to insert new records
        if len(gym_details):
            DeleteQuery(GymMember).where(GymMember.gym_id << gym_details.keys()).execute()

        # insert new gym members
        if len(gym_members):
            bulk_upsert(GymMember, gym_members)

    log.info('Upserted %d gyms and %d gym members',
             len(gym_details),
             len(gym_members))

def parse_pokestops(args, pokestop_responses):
    pokestop_infos = {}

    for p in pokestop_responses.values():
        p_id = p['fort_id']
        p_img = p['image_urls'][0].replace('http://', '').replace('https://', '')
        pokestop_infos[p_id] = {
            'pokestop_id': p_id,
            'name': p['name'],
            'description': p.get('description'),
            'image_url': p_img,
            'last_scanned': datetime.utcnow()
        }

    if len(pokestop_infos):
        bulk_upsert(PokestopDetails, pokestop_infos)

    log.info('Upserted %d pokestop infos',
             len(pokestop_infos))


def db_updater(args, q):
    # The forever loop
    while True:
        try:

            while True:
                try:
                    flaskDb.connect_db()
                    break
                except Exception as e:
                    log.warning('%s... Retrying', e)
                    time.sleep(5)

            # Loop the queue
            while True:
                model, data = q.get()
                bulk_upsert(model, data)
                q.task_done()
                log.debug('Upserted to %s, %d records (upsert queue remaining: %d)',
                          model.__name__,
                          len(data),
                          q.qsize())
                if q.qsize() > 50:
                    log.warning("DB queue is > 50 (@%d); try increasing --db-threads", q.qsize())

        except Exception as e:
            log.exception('Exception in db_updater: %s', e)
            time.sleep(5)


def clean_db_loop(args):
    while True:
        try:
            # Clean out old scanned locations
            query = (ScannedLocation
                     .delete()
                     .where((ScannedLocation.last_modified <
                             (datetime.utcnow() - timedelta(minutes=30)))))
            query.execute()

            query = (MainWorker
                     .delete()
                     .where((ScannedLocation.last_modified <
                             (datetime.utcnow() - timedelta(minutes=30)))))
            query.execute()

            query = (WorkerStatus
                     .delete()
                     .where((ScannedLocation.last_modified <
                             (datetime.utcnow() - timedelta(minutes=30)))))
            query.execute()

            # Remove active modifier from expired lured pokestops
            query = (Pokestop
                     .update(lure_expiration=None, active_fort_modifier=None, player_lure=None)
                     .where(Pokestop.lure_expiration < datetime.utcnow()))
            query.execute()

            # If desired, clear old pokemon spawns
            if args.purge_data > 0:
                log.info("Beginning purge of old Pokemon spawns.")
                start = datetime.utcnow()
                query = (Pokemon
                         .delete()
                         .where((Pokemon.disappear_time <
                                (datetime.utcnow() - timedelta(hours=args.purge_data))) & ~(Pokemon.time_detail == 1)))
                rows = query.execute()
                end = datetime.utcnow()
                diff = end-start
                log.info("Completed purge of old Pokemon spawns. "
                         "%i deleted in %f seconds.",
                         rows, diff.total_seconds())

            log.info('Regular database cleaning complete')
            time.sleep(60)
        except Exception as e:
            log.exception('Exception in clean_db_loop: %s', e)


def bulk_upsert(cls, data):
    num_rows = len(data.values())
    i = 0

    if args.db_type == 'mysql':
        step = 120
    else:
        # SQLite has a default max number of parameters of 999,
        # so we need to limit how many rows we insert for it.
        step = 50

    while i < num_rows:
        log.debug('Inserting items %d to %d', i, min(i + step, num_rows))
        try:
            InsertQuery(cls, rows=data.values()[i:min(i + step, num_rows)]).upsert().execute()
        except Exception as e:
            log.warning('%s... Retrying', e)
            continue

        i += step

# OLD POKESTOP SPIN
def spin_pokestop(api, fort, step_location):
    spinning_radius = 0.04
    if in_radius((fort['latitude'], fort['longitude']), step_location,
                 spinning_radius):
        log.debug('Attempt to spin Pokestop (ID %s)', fort['id'])
        spin_try = 0
        while spin_try < 3:
            spin_try += 1
            time.sleep(random.uniform(0.8, 1.8))  # Do not let Niantic throttle
            spin_response = spin_pokestop_request(api, fort, step_location)
            time.sleep(random.uniform(2, 4))  # Do not let Niantic throttle

            # Check for reCaptcha
            captcha_url = spin_response['responses']['CHECK_CHALLENGE']['challenge_url']
            if len(captcha_url) > 1:
                log.warning('Account encountered a reCaptcha.')
                return False

            spin_result = spin_response['responses']['FORT_SEARCH']['result']
            if spin_result is 1:
                log.warning('Successful Pokestop spin.')
                return True
            elif spin_result is 2:
                log.warning('Pokestop was not in range to spin.')
            elif spin_result is 3:
                log.info('Pokestop has already been recently spun.')
            elif spin_result is 4:
                log.warning('Failed to spin Pokestop. Inventory is full.')
            elif spin_result is 5:
                log.warning('Pokestop is not spinable. Already spun maximum number for this day, bot?')
            else:
                log.warning('Failed to spin a Pokestop. Unknown result %d.', spin_result)

    return False


def spin_pokestop_request(api, fort, step_location):
    try:
        req = api.create_request()
        spin_pokestop_response = req.fort_search(
            fort_id=fort['id'],
            fort_latitude=fort['latitude'],
            fort_longitude=fort['longitude'],
            player_latitude=step_location[0],
            player_longitude=step_location[1])
        spin_pokestop_response = req.check_challenge()
        spin_pokestop_response = req.get_hatched_eggs()
        spin_pokestop_response = req.get_inventory()
        spin_pokestop_response = req.check_awarded_badges()
        spin_pokestop_response = req.download_settings()
        spin_pokestop_response = req.get_buddy_walked()
        spin_pokestop_response = req.call()

        return spin_pokestop_response

    except Exception as e:
        log.warning('Exception while spinning Pokestop: %s', e)
        return False


def create_tables(db):
    db.connect()
    verify_database_schema(db)
    db.create_tables([Pokemon, Pokestop, PokestopDetails, Gym, ScannedLocation, GymDetails, GymMember, GymPokemon, Trainer, MainWorker, WorkerStatus], safe=True)
    db.close()


def drop_tables(db):
    db.connect()
    db.drop_tables([Pokemon, Pokestop, PokestopDetails, Gym, ScannedLocation, Versions, GymDetails, GymMember, GymPokemon, Trainer, MainWorker, WorkerStatus, Versions], safe=True)
    db.close()


def verify_database_schema(db):
    if not Versions.table_exists():
        db.create_tables([Versions])

        if ScannedLocation.table_exists():
            # Versions table didn't exist, but there were tables. This must mean the user
            # is coming from a database that existed before we started tracking the schema
            # version. Perform a full upgrade.
            InsertQuery(Versions, {Versions.key: 'schema_version', Versions.val: 0}).execute()
            database_migrate(db, 0)
        else:
            InsertQuery(Versions, {Versions.key: 'schema_version', Versions.val: db_schema_version}).execute()

    else:
        db_ver = Versions.get(Versions.key == 'schema_version').val

        if db_ver < db_schema_version:
            database_migrate(db, db_ver)

        elif db_ver > db_schema_version:
            log.error("Your database version (%i) appears to be newer than the code supports (%i).",
                      db_ver, db_schema_version)
            log.error("Please upgrade your code base or drop all tables in your database.")
            sys.exit(1)


def database_migrate(db, old_ver):
    # Update database schema version
    Versions.update(val=db_schema_version).where(Versions.key == 'schema_version').execute()

    log.info("Detected database version %i, updating to %i", old_ver, db_schema_version)

    # Perform migrations here
    migrator = None
    if args.db_type == 'mysql':
        migrator = MySQLMigrator(db)
    else:
        migrator = SqliteMigrator(db)

#   No longer necessary, we're doing this at schema 4 as well
#    if old_ver < 1:
#        db.drop_tables([ScannedLocation])

    if old_ver < 2:
        migrate(migrator.add_column('pokestop', 'encounter_id', CharField(max_length=50, null=True)))

    if old_ver < 3:
        migrate(
            migrator.add_column('pokestop', 'active_fort_modifier', CharField(max_length=50, null=True)),
            migrator.drop_column('pokestop', 'encounter_id'),
            migrator.drop_column('pokestop', 'active_pokemon_id')
        )

    if old_ver < 4:
        db.drop_tables([ScannedLocation])

    if old_ver < 5:
        # Some pokemon were added before the 595 bug was "fixed"
        # Clean those up for a better UX
        query = (Pokemon
                 .delete()
                 .where(Pokemon.disappear_time >
                        (datetime.utcnow() - timedelta(hours=24))))
        query.execute()

    if old_ver < 6:
        migrate(
            migrator.add_column('gym', 'last_scanned', DateTimeField(null=True)),
        )

    if old_ver < 7:
        migrate(
            migrator.drop_column('gymdetails', 'description'),
            migrator.add_column('gymdetails', 'description', TextField(null=True, default=""))
        )

    if old_ver < 8:
        migrate(
            migrator.add_column('pokemon', 'individual_attack', IntegerField(null=True, default=0)),
            migrator.add_column('pokemon', 'individual_defense', IntegerField(null=True, default=0)),
            migrator.add_column('pokemon', 'individual_stamina', IntegerField(null=True, default=0)),
            migrator.add_column('pokemon', 'move_1', IntegerField(null=True, default=0)),
            migrator.add_column('pokemon', 'move_2', IntegerField(null=True, default=0))
        )

    if old_ver < 9:
        migrate(
            migrator.add_column('pokemon', 'last_modified', DateTimeField(null=True, index=True)),
            migrator.add_column('pokestop', 'last_updated', DateTimeField(null=True, index=True))
        )
    if old_ver < 10:
        migrate(
            migrator.add_column('pokemon', 'time_detail', IntegerField(default=-1, index=True))
        )

    if old_ver < 11:
        migrate(
            migrator.add_column('workerstatus', 'captchas', IntegerField(default=0))
        )

    if old_ver < 12:
        migrate(
            migrator.add_column('scannedlocation', 'username', CharField(max_length=255, null=False, default=" ")),
        )

    if old_ver < 13:
        migrate(
            migrator.drop_not_null('pokemon', 'spawnpoint_id'),
            migrator.add_column('pokemon', 'pokestop_id', CharField(null=True))
        )

    if old_ver < 14:
        migrate(
            migrator.add_column('pokemon', 'weight', DoubleField(null=True, default=0)),
            migrator.add_column('pokemon', 'height', DoubleField(null=True, default=0)),
            migrator.add_column('pokemon', 'gender', IntegerField(null=True, default=0))
        )

    if old_ver < 15:
        migrate(
            migrator.add_column('pokemon', 'form', SmallIntegerField(null=True))
        )

    if old_ver < 16:
        migrate(
            migrator.add_column('pokemon', 'previous_id', SmallIntegerField(null=True))
        )

    if old_ver < 17:
        db.create_tables([PokestopDetails], safe=True)

    if old_ver < 18:
        migrate(
            migrator.add_column('pokestop', 'player_lure', CharField(null=True))
        )

    if old_ver < 19:
        migrate(
            migrator.add_column('gym', 'is_active', SmallIntegerField(null=True)),
            migrator.add_column('gym', 'train_battle', SmallIntegerField(null=True))
        )

    if old_ver < 20:
        log.info('This DB schema update can take some time. Please be patient.')

        # change some column types from INT to SMALLINT
        # we don't have to touch sqlite because it has INTEGER only
        if args.db_type == 'mysql':
            db.execute_sql(
                'ALTER TABLE `pokemon` '
                'MODIFY COLUMN `pokemon_id` SMALLINT NOT NULL,'
                'MODIFY COLUMN `individual_attack` SMALLINT '
                'NULL DEFAULT NULL,'
                'MODIFY COLUMN `individual_defense` SMALLINT '
                'NULL DEFAULT NULL,'
                'MODIFY COLUMN `individual_stamina` SMALLINT '
                'NULL DEFAULT NULL,'
                'MODIFY COLUMN `move_1` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `move_2` SMALLINT NULL DEFAULT NULL;'
            )
            db.execute_sql(
                'ALTER TABLE `gym` '
                'MODIFY COLUMN `team_id` SMALLINT NOT NULL,'
                'MODIFY COLUMN `guard_pokemon_id` SMALLINT NOT NULL;'
            )
            db.execute_sql(
                'ALTER TABLE `versions` '
                'MODIFY COLUMN `val` SMALLINT NOT NULL;'
            )
            db.execute_sql(
                'ALTER TABLE `gympokemon` '
                'MODIFY COLUMN `pokemon_id` SMALLINT NOT NULL,'
                'MODIFY COLUMN `cp` SMALLINT NOT NULL,'
                'MODIFY COLUMN `num_upgrades` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `move_1` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `move_2` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `stamina` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `stamina_max` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `iv_defense` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `iv_stamina` SMALLINT NULL DEFAULT NULL,'
                'MODIFY COLUMN `iv_attack` SMALLINT NULL DEFAULT NULL;'
            )
            db.execute_sql(
                'ALTER TABLE `trainer` '
                'MODIFY COLUMN `team` SMALLINT NOT NULL,'
                'MODIFY COLUMN `level` SMALLINT NOT NULL;'
            )

        # add some missing indexes
        migrate(
            migrator.add_index('gym', ('last_scanned',), False),
            migrator.add_index('gymmember', ('last_scanned',), False),
            migrator.add_index('gymmember', ('pokemon_uid',), False),
            migrator.add_index('gympokemon', ('trainer_name',), False),
            migrator.add_index('pokestop', ('active_fort_modifier',), False),
        )
        # pokestop.last_updated was missing in a previous migration
        # check whether we have to add it
        has_last_updated_index = False
        for index in db.get_indexes('pokestop'):
            if index.columns[0] == 'last_updated':
                has_last_updated_index = True
                break
        if not has_last_updated_index:
            log.debug('pokestop.last_updated index is missing. Creating now.')
            migrate(
                migrator.add_index('pokestop', ('last_updated',), False)
            )

    log.info('Schema upgrade complete.')
