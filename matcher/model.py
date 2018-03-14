# coding: utf-8
from sqlalchemy import func
from sqlalchemy.schema import ForeignKeyConstraint, ForeignKey, Column
from sqlalchemy.types import BigInteger, Float, Integer, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.associationproxy import association_proxy
from geoalchemy2 import Geography  # noqa: F401
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import relationship, backref, column_property
from sqlalchemy.sql.expression import cast
from .database import session
from flask_login import UserMixin
from . import wikidata, matcher, match, wikipedia
from .overpass import oql_from_tag
from collections import defaultdict

Base = declarative_base()
Base.query = session.query_property()

osm_api_base = 'https://api.openstreetmap.org/api/0.6'

osm_type_enum = postgresql.ENUM('node', 'way', 'relation',
                                name='osm_type_enum',
                                metadata=Base.metadata)

# also check for tags that start with 'disused:'
disused_prefix_key = {'amenity', 'railway', 'leisure', 'tourism',
                      'man_made', 'shop', 'building'}

class User(Base, UserMixin):
    __tablename__ = 'user'
    id = Column(Integer, primary_key=True)
    username = Column(String)
    password = Column(String)
    name = Column(String)
    email = Column(String)
    active = Column(Boolean, default=True)
    sign_up = Column(DateTime, default=func.now())
    is_admin = Column(Boolean, default=False)
    description = Column(Text)
    img = Column(String)
    languages = Column(postgresql.ARRAY(String))

    def is_active(self):
        return self.active

# states: wikipedia, tags, wbgetentities, overpass, postgis, osm2pgsql, ready
# bad state: overpass_fail

class IsA(Base):
    __tablename__ = 'isa'
    item_id = Column(Integer, primary_key=True, autoincrement=False)
    entity = Column(postgresql.JSON)
    qid = column_property('Q' + cast(item_id, String))
    subclass_of = Column(postgresql.ARRAY(String))

    def label(self, lang='en'):
        labels = self.entity['labels']
        if lang in labels:
            return labels[lang]['value']
        elif lang != 'en' and 'en' in labels:
            return labels['en']['value']
        elif labels:
            return list(labels.values())[0]['value']

class ItemIsA(Base):
    __tablename__ = 'item_isa'
    item_id = Column(Integer,
                     ForeignKey('item.item_id'),
                     primary_key=True,
                     autoincrement=False)
    isa_id = Column(Integer,
                     ForeignKey('isa.item_id'),
                     primary_key=True,
                     autoincrement=False)

    item = relationship('Item')
    isa = relationship('IsA')

class Item(Base):
    __tablename__ = 'item'

    item_id = Column(Integer, primary_key=True, autoincrement=False)
    location = Column(Geography('POINT', spatial_index=True), nullable=False)
    enwiki = Column(String, index=True)
    entity = Column(postgresql.JSON)
    categories = Column(postgresql.ARRAY(String))
    old_tags = Column(postgresql.ARRAY(String))
    qid = column_property('Q' + cast(item_id, String))
    ewkt = column_property(func.ST_AsEWKT(location), deferred=True)
    query_label = Column(String, index=True)
    extract = Column(String)
    extract_names = Column(postgresql.ARRAY(String))

    db_tags = relationship('ItemTag',
                           collection_class=set,
                           cascade='save-update, merge, delete, delete-orphan',
                           backref='item')

    tags = association_proxy('db_tags', 'tag_or_key')

    isa = relationship('ItemIsA')

    @property
    def labels(self):
        if not self.entity:
            return None

        return {l['language']: l['value']
                for l in self.entity['labels'].values()}

    def label(self, lang='en'):
        if not self.entity:
            return self.enwiki or self.query_label or None

        labels = self.entity['labels']
        if lang in labels:
            return labels[lang]['value']
        elif lang != 'en' and 'en' in labels:
            return labels['en']['value']
        elif labels:
            return list(labels.values())[0]['value']

    def label_best_language(self, languages):
        if not languages:
            return self.label()
        labels = self.entity['labels']
        for lang in languages:
            code = code if isinstance(code, string) else lang.wikimedia_language_code
            if code in labels:
                return labels[code]['value']
        return self.label()

    @classmethod
    def get_by_qid(cls, qid):
        if qid and len(qid) > 1 and qid[0].upper() == 'Q' and qid[1:].isdigit():
            return cls.query.get(qid[1:])

    def label_and_qid(self, lang='en'):
        label = self.label(lang=lang)
        return '{label} ({item.qid})'.format(label=label, item=self)

    @property
    def wikidata_uri(self):
        return 'https://www.wikidata.org/wiki/Q{}'.format(self.item_id)

    def get_lat_lon(self):
        return session.query(func.ST_Y(self.location),
                             func.ST_X(self.location)).one()

    def get_osm_url(self, zoom=18):
        lat, lon = self.get_lat_lon()
        return f'https://www.openstreetmap.org/#map={zoom}/{lat}/{lon}'

    def get_extra_tags(self):
        tags = set()
        for item_id in self.instanceof():
            for tag in wikidata.extra_keys.get('Q{:d}'.format(item_id), []):
                if tag:
                    tags.add(tag[4:])
        return tags

    @property
    def ref_keys(self):
        return {f'ref:nrhp={v}' for v in (self.ref_nrhp() or [])}

    def disused_tags(self):
        tags = set()
        prefixes = ('disused', 'was', 'abandoned')
        for i in self.tags:
            key = i.split('=')[0] if '=' in i else i
            if key in disused_prefix_key:
                tags |= {prefix + ':' + i for prefix in prefixes}
        return tags

    def hstore_query(self, ignore_tags=None):
        '''hstore query for use with osm2pgsql database'''
        ignore_tags = set(ignore_tags or [])

        # On Wikidata the item for 'facility' (Q13226383), has an OSM key of
        # 'amenity'. This is too generic, so we ignore it.
        ignore_tags.add('amenity')
        tags = (self.get_extra_tags() |
                set(self.tags) |
                self.ref_keys |
                self.disused_tags()) - ignore_tags
        if not tags:
            return

        cond = ("((tags->'{}') = '{}')".format(*tag.split('='))
                if '=' in tag
                else "(tags ? '{}')".format(tag) for tag in tags)
        return ' or\n '.join(cond)

    def instanceof(self):
        if self.entity:
            return [i['mainsnak']['datavalue']['value']['numeric-id']
                    for i in self.entity['claims'].get('P31', [])]
        else:
            return []

    def get_item_identifiers(self):
        if not self.entity:
            return {}

        property_map = [
            ('P238', 'iata', 'IATA airport code'),
            ('P239', 'icao', 'ICAO airport code'),
            ('P240', 'faa', 'FAA airport code'),
            ('P296', 'ref', 'station code'),
            ('P722', 'uic_ref', 'UIC station code'),
            ('P2815', 'ref', 'ESR station code'),
            ('P1216', 'HE_ref', 'National Heritage List for England number'),
            ('P4755', 'ref', 'UK railway station code'),
            ('P4803', 'ref', 'Amtrak station code'),
        ]

        tags = {}
        for claim, osm_key, label in property_map:
            values = [i['mainsnak']['datavalue']['value']
                      for i in self.entity['claims'].get(claim, [])]
            if values:
                tags[osm_key] = (values, label)
        return tags

    def ref_nrhp(self):
        if self.entity:
            return [i['mainsnak']['datavalue']['value']
                    for i in self.entity['claims'].get('P649', [])]
        else:
            return []

    def names(self):
        d = wikidata.names_from_entity(self.entity) or defaultdict(list)
        for name in self.extract_names or []:
            d[name].append(('extract', 'enwiki'))
        return d or None

    def refresh_extract_names(self):
        self.extract_names = wikipedia.html_names(self.extract)

    def get_oql(self):
        lat, lon = session.query(func.ST_Y(self.location), func.ST_X(self.location)).one()
        union = []
        for tag in self.tags:
            osm_filter = 'around:1000,{:f},{:f}'.format(lat, lon)
            union += oql_from_tag(tag, False, osm_filter)
        return union

    def coords(self):
        return session.query(func.ST_Y(self.location), func.ST_X(self.location)).one()

    def image_filenames(self):
        return [i['mainsnak']['datavalue']['value']
                for i in self.entity['claims'].get('P18', [])]

    def defunct_cats(self):
        words = {'demolish', 'disestablishment', 'defunct', 'abandon', 'mothballed',
                 'decommission', 'former', 'dismantled', 'disused', 'disassembled',
                 'abandoned', 'disband', 'scrapped', 'unused', 'closed', 'condemned'}

        exclude = {'Defunct baseball venues in the United States',
                   'Defunct National Football League venues'}

        found = []
        for item_cat in self.categories or []:
            if item_cat in exclude:
                continue
            lc_item_cat = item_cat.lower()
            found += [item_cat for i in words if i in lc_item_cat]
        return found

    @property
    def criteria(self):
        return {('Tag:' if '=' in t else 'Key:') + t for t in self.tags or []}

    @property
    def category_map(self):
        if self.categories:
            return matcher.categories_to_tags_map(self.categories)

    def sitelinks(self):
        if self.entity:
            return self.entity.get('sitelinks')

    def is_proposed(self):
        '''is this item a proposed building or structure?'''

        cats = self.categories or []
        if any(cat.startswith('Proposed ') for cat in cats):
            return True
        # proposed building or structure (Q811683)
        return 'Q811683' in (self.instanceof() or [])

    def skip_item_during_match(self):
        ''' cebwiki and svwiki contain lots of poor quality stubs
        best to skip items that are only cebwiki or cebwiki + svwiki
        '''
        if self.is_proposed():  # skip proposed building or structure
            return True
        if not self.entity:
            return False
        sitelinks = self.entity.get('sitelinks')
        if not sitelinks:
            return False
        sites = set(sitelinks.keys())
        return sites == {'cebwiki'} or sites == {'cebwiki', 'svwiki'}

    def get_names(self):
        item = self.entity
        if not item:
            return

        names = defaultdict(list)
        skip_lang = {'ar', 'arc', 'pl'}
        # only include aliases if there are less than 6 other names
        if len(item.get('sitelinks', {})) < 6 and len(item['labels']) < 6:
            for k, v in item.get('aliases', {}).items():
                if k in skip_lang:
                    continue
                if len(v) > 3:
                    continue
                for name in v:
                    names[name].append(('alias', k))
        for k, v in item['labels'].items():
            if k in skip_lang:
                continue
            names[v].append(('label', k))
        for k, v in item.get('sitelinks', {}).items():
            if k + 'wiki' in skip_lang:
                continue
            names[v].append(('sitelink', k))
        return names

class ItemTag(Base):
    __tablename__ = 'item_tag'

    item_id = Column(Integer, ForeignKey('item.item_id'), primary_key=True)
    tag_or_key = Column(String, primary_key=True, index=True)

    def __init__(self, tag_or_key):
        self.tag_or_key = tag_or_key

class PlaceItem(Base):
    __tablename__ = 'place_item'

    item_id = Column(Integer, ForeignKey('item.item_id'), primary_key=True)
    osm_type = Column(osm_type_enum, primary_key=True)
    osm_id = Column(BigInteger, primary_key=True)
    place_id = Column(BigInteger)

    __table_args__ = (
        ForeignKeyConstraint(
            ['osm_type', 'osm_id'],
            ['place.osm_type', 'place.osm_id']
        ),
    )

    item = relationship('Item')
    place = relationship('Place')

class ItemCandidate(Base):
    __tablename__ = 'item_candidate'

    item_id = Column(Integer, ForeignKey('item.item_id'), primary_key=True)
    osm_id = Column(BigInteger, primary_key=True)
    osm_type = Column(osm_type_enum, primary_key=True)
    name = Column(String)
    dist = Column(Float)
    tags = Column(postgresql.JSON)
    planet_table = Column(String)
    src_id = Column(BigInteger)

    item = relationship('Item', backref=backref('candidates',
                                                lazy='dynamic',
                                                cascade='save-update, merge, delete, delete-orphan'))

    @property
    def key(self):
        return f'Q{self.item_id}-{self.osm_type:s}-{self.osm_id:d}'

    def get_match(self):
        endings = matcher.get_ending_from_criteria(self.tags)
        wikidata_names = self.item.names()
        return match.check_for_match(self.tags, wikidata_names, endings)

    def get_all_matches(self):
        endings = matcher.get_ending_from_criteria(self.item.tags)
        wikidata_names = self.item.names()
        m = match.get_all_matches(self.tags, wikidata_names, endings)
        return m

    def languages(self):
        return {key[5:] for key in self.tags.keys()
                if key.startswith('name:')}

    def matching_tags(self):
        tags = []

        for tag_or_key in self.item.tags:
            if '=' not in tag_or_key and tag_or_key in self.tags:
                tags.append(tag_or_key)
                continue
            key, _, value = tag_or_key.partition('=')
            if self.tags.get(key) == value:
                tags.append(tag_or_key)
                continue

        return tags

    def update(self, candidate):
        for k, v in candidate.items():
            if k in {'osm_id', 'osm_type'}:
                continue
            setattr(self, k, v)

    @property
    def wikidata_tag(self):
        return self.tags.get('wikidata') or None

    def label_best_language(self, languages):
        if not languages:
            return self.label

        for key in 'bridge:name', 'lock_name':
            if key in self.tags:
                return self.tags[key]

        for lang in languages:
            key = 'name:' + (lang if isinstance(lang) else lang.iso_639_1)
            if key in self.tags:
                return self.tags[key]

        return self.label

    @property
    def label(self):
        for key in 'bridge:name', 'lock_name':
            if key in self.tags:
                return self.tags[key]

        if 'name' in self.tags:
            name = self.tags['name']
            if 'addr:housename' in self.tags:
                return '{} (house name: {})'.format(name, self.tags['addr:housename'])
            else:
                return name

        if 'name:en' in self.tags:
            return self.tags['name:en']
        for k, v in self.tags.items():
            if k.startswith('name:'):
                return v
        for k, v in self.tags.items():
            if 'name' in k:
                return v
        return '{}/{}'.format(self.osm_type, self.osm_id)

    @property
    def url(self):
        return '{}/{}/{}'.format(osm_api_base, self.osm_type, self.osm_id)

# class ItemCandidateTag(Base):
#     __tablename__ = 'item_candidate_tag'
#     __table_args__ = (
#         ForeignKeyConstraint(['item_id', 'osm_id', 'osm_type'],
#                              [ItemCandidate.item_id,
#                               ItemCandidate.osm_id,
#                               ItemCandidate.osm_type]),
#     )
#
#     item_id = Column(Integer, primary_key=True)
#     osm_id = Column(BigInteger, primary_key=True)
#     osm_type = Column(osm_type_enum, primary_key=True)
#     k = Column(String, primary_key=True)
#     v = Column(String, primary_key=True)
#
#     item_candidate = relationship(ItemCandidate,
#                                   backref=backref('tag_table', lazy='dynamic'))

class TagOrKey(Base):
    __tablename__ = 'tag_or_key'

    name = Column(String, primary_key=True)
    count_all = Column(Integer)

class Category(Base):
    __tablename__ = 'category'

    name = Column(String, primary_key=True)
    page_count = Column(Integer)

class Changeset(Base):
    __tablename__ = 'changeset'
    id = Column(BigInteger, primary_key=True)
    created = Column(DateTime)
    place_id = Column(BigInteger)
    osm_type = Column(osm_type_enum, index=True)
    osm_id = Column(BigInteger, index=True)
    item_id = Column(Integer)
    comment = Column(String)
    user_id = Column(Integer, ForeignKey(User.id))
    update_count = Column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ['osm_type', 'osm_id'],
            ['place.osm_type', 'place.osm_id']
        ),
    )

    user = relationship('User',
                        backref=backref('changesets',
                                        lazy='dynamic',
                                        order_by='Changeset.created.desc()'))
    place = relationship('Place',
                         backref=backref('changesets',
                                        lazy='dynamic',
                                        order_by='Changeset.created.desc()'))

    @property
    def item_label(self):
        item = Item.query.get(self.item_id)
        if item:
            return item.label()

class ChangesetEdit(Base):
    __tablename__ = 'changeset_edit'
    __table_args__ = (
        ForeignKeyConstraint(['item_id', 'osm_id', 'osm_type'],
                             [ItemCandidate.item_id,
                              ItemCandidate.osm_id,
                              ItemCandidate.osm_type]),
    )

    changeset_id = Column(BigInteger,
                          ForeignKey('changeset.id'),
                          primary_key=True)
    item_id = Column(Integer, primary_key=True)
    osm_id = Column(BigInteger, primary_key=True)
    osm_type = Column(osm_type_enum, primary_key=True)

    changeset = relationship('Changeset',
                             backref=backref('matches', lazy='dynamic'))

class BadMatch(Base):
    __tablename__ = 'bad_match'
    __table_args__ = (
        ForeignKeyConstraint(['item_id', 'osm_id', 'osm_type'],
                             [ItemCandidate.item_id,
                              ItemCandidate.osm_id,
                              ItemCandidate.osm_type]),
    )

    item_id = Column(Integer, primary_key=True)
    osm_id = Column(BigInteger, primary_key=True)
    osm_type = Column(osm_type_enum, primary_key=True)
    user_id = Column(Integer, ForeignKey(User.id), primary_key=True)
    created = Column(DateTime, default=func.now())
    comment = Column(Text)

    item_candidate = relationship(ItemCandidate,
                                  backref=backref('bad_matches', lazy='dynamic'))
    user = relationship(User, backref=backref('bad_matches', lazy='dynamic'))

class Timing(Base):
    __tablename__ = 'timing'
    id = Column(Integer, primary_key=True)
    start = Column(Float, nullable=False)
    path = Column(String, nullable=False)
    name = Column(String, nullable=False)
    seconds = Column(Float, nullable=False)

def get_bad(items):
    if not items:
        return {}
    q = (session.query(BadMatch.item_id)
                .filter(BadMatch.item_id.in_([i.item_id for i in items])))
    return {item_id for item_id, in q}

class Language(Base):
    __tablename__ = 'language'
    item_id = Column(Integer, primary_key=True, autoincrement=False)
    iso_639_1 = Column(String(2))
    iso_639_2 = Column(String(3))
    iso_639_3 = Column(String(3))
    wikimedia_language_code = Column(String, unique=True)
    qid = column_property('Q' + cast(item_id, String))
    labels = relationship('LanguageLabel',
                          lazy='dynamic',
                          foreign_keys=lambda: LanguageLabel.item_id)

    def english_name(self):
        return self.labels.filter_by(wikimedia_language_code='en').one().label

    def self_name(self):
        ''' Name of this language in this language. '''
        return self.labels.filter_by(language=self).one().label

    def label(self):
        name = self.self_name()
        if self.wikimedia_language_code != 'en':
            name += ' / ' + self.english_name()
        return f'{name} [{self.iso_639_1}]'

class LanguageLabel(Base):
    __tablename__ = 'language_label'
    item_id = Column(Integer,
                     ForeignKey(Language.item_id),
                     primary_key=True,
                     autoincrement=False)
    wikimedia_language_code = Column(String,
                                     ForeignKey(Language.wikimedia_language_code),
                                     primary_key=True)
    label = Column(String, nullable=False)

    language = relationship('Language', foreign_keys=[wikimedia_language_code])
