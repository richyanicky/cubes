# -*i coding=utf -*-
# Actually, this is a furry snowflake, not a nice star

from cubes.browser import *
from cubes.common import get_logger
from .mapper import SnowflakeMapper, DenormalizedMapper, coalesce_physical, DEFAULT_KEY_FIELD
import logging
import collections
import re
import datetime
from cubes.errors import *
from cubes.computation import *
from cubes.backends.sql import extensions
from cubes import statutils
from cubes.model import Attribute

try:
    import sqlalchemy
    import sqlalchemy.sql as sql

    aggregation_functions = {
        "sum": sql.functions.sum,
        "min": sql.functions.min,
        "max": sql.functions.max,
        "count": sql.functions.count,
        "avg": extensions.avg,
        "stddev": extensions.stddev,
        "variance": extensions.variance,
        "identity": lambda c: c
    }

    calculated_aggregation_functions = {
        "sma": statutils.simple_moving_average_factory,
        "wma": statutils.weighted_moving_average_factory
    }

except ImportError:
    from cubes.common import MissingPackage
    sqlalchemy = sql = MissingPackage("sqlalchemy", "SQL aggregation browser")
    aggregation_functions = {}

__all__ = [
    "SnowflakeBrowser",
    "QueryContext"
]

_EXPR_EVAL_NS = {
    "sqlalchemy": sqlalchemy,
    "sql": sql,
    "func": sql.expression.func,
    "case": sql.expression.case,
    "text": sql.expression.text,
    "datetime" : datetime,
    "re": re
}

class SnowflakeBrowser(AggregationBrowser):
    """docstring for SnowflakeBrowser"""

    def __init__(self, cube, connectable=None, locale=None, metadata=None,
                 debug=False, **options):
        """SnowflakeBrowser is a SQL-based AggregationBrowser implementation that
        can aggregate star and snowflake schemas without need of having
        explicit view or physical denormalized table.

        Attributes:

        * `cube` - browsed cube
        * `connectable` - SQLAlchemy connectable object (engine or connection)
        * `locale` - locale used for browsing
        * `metadata` - SQLAlchemy MetaData object
        * `debug` - output SQL to the logger at INFO level
        * `options` - passed to the mapper and context (see their respective
          documentation)

        Tuning:

        * `include_summary` - it ``True`` then summary is included in
          aggregation result. Turned on by default.
        * `include_cell_count` – if ``True`` then total cell count is included
          in aggregation result. Turned on by default.
          performance reasons

        Limitations:

        * only one locale can be used for browsing at a time
        * locale is implemented as denormalized: one column for each language

        """
        super(SnowflakeBrowser, self).__init__(cube)

        if cube == None:
            raise ArgumentError("Cube for browser should not be None.")

        self.logger = get_logger()

        self.cube = cube
        self.locale = locale or cube.model.locale
        self.debug = debug

        if connectable is not None:
            self.connectable = connectable
            self.metadata = metadata or sqlalchemy.MetaData(bind=self.connectable)

        self.include_summary = options.get("include_summary", True)
        self.include_cell_count = options.get("include_cell_count", True)
        # Mapper is responsible for finding corresponding physical columns to
        # dimension attributes and fact measures. It also provides information
        # about relevant joins to be able to retrieve certain attributes.

        if options.get("use_denormalization"):
            mapper_class = DenormalizedMapper
        else:
            mapper_class = SnowflakeMapper

        self.logger.debug("using mapper %s for cube '%s' (locale: %s)" % \
                            (str(mapper_class.__name__), cube.name, locale))

        self.mapper = mapper_class(cube, locale=self.locale, **options)
        self.logger.debug("mapper schema: %s" % self.mapper.schema)

        # QueryContext is creating SQL statements (using SQLAlchemy). It
        # also caches information about tables retrieved from metadata.
        # FIXME: new context is created also when locale changes in set_locale
        self.options = options
        self.context = QueryContext(self.cube, self.mapper,
                                      metadata=self.metadata, **self.options)

    def set_locale(self, locale):
        """Change the browser's locale"""
        self.logger.debug("changing browser's locale to %s" % locale)
        self.mapper.set_locale(locale)
        self.locale = locale
        # Reset context
        self.context = QueryContext(self.cube, self.mapper,
                                      metadata=self.metadata, **self.options)

    def fact(self, key_value):
        """Get a single fact with key `key_value` from cube.

        Number of SQL queries: 1."""

        select = self.context.fact_statement(key_value)

        if self.debug:
            self.logger.info("fact SQL:\n%s" % select)

        cursor = self.connectable.execute(select)
        row = cursor.fetchone()

        labels = self.context.logical_labels(select.columns)

        if row:
            # Convert SQLAlchemy object into a dictionary
            record = dict(zip(labels, row))
        else:
            record = None

        cursor.close()

        return record

    def facts(self, cell, order=None, page=None, page_size=None):
        """Return all facts from `cell`, might be ordered and paginated.

        Number of SQL queries: 1.
        """

        attributes = set()
        attributes |= set(self.cube.details)
        for dim in self.cube.dimensions:
            attributes |= set(dim.hierarchy().all_attributes())
        # all measures that fit the bill
        attributes |= set([ m for m in self.cube.measures if 'identity' not in m.aggregations ])

        cond = self.context.condition_for_cell(cell)
        statement = self.context.denormalized_statement(
            attributes=attributes,
            include_fact_key=True,
            condition_attributes=cond.attributes
        )

        if cond.condition is not None:
            statement = statement.where(cond.condition)

        statement = self.context.paginated_statement(statement, page, page_size)
        # FIXME: use level based ordering here. What levels to consider? In
        # what order?
        statement = self.context.ordered_statement(statement, order)

        if self.debug:
            self.logger.info("facts SQL:\n%s" % statement)

        result = self.connectable.execute(statement)
        labels = self.context.logical_labels(statement.columns)

        return ResultIterator(result, labels)

    def values(self, cell, dimension, depth=None, hierarchy=None, page=None,
               page_size=None, order=None, **options):
        """Return values for `dimension` with level depth `depth`. If `depth`
        is ``None``, all levels are returned.

        Number of database queries: 1.
        """
        dimension = self.cube.dimension(dimension)
        hierarchy = dimension.hierarchy(hierarchy)

        levels = hierarchy.levels

        if depth == 0:
            raise ArgumentError("Depth for dimension values should not be 0")
        elif depth is not None:
            levels = levels[0:depth]

        # TODO: this might unnecessarily add fact table as well, there might
        #       be cases where we do not want that (hm, might be? really? note
        #       the cell)

        attributes = []
        for level in levels:
            attributes.extend(level.attributes)

        cond = self.context.condition_for_cell(cell)

        statement = self.context.denormalized_statement(attributes=attributes,
                                                        include_fact_key=False,
                                                        condition_attributes=
                                                              cond.attributes)
        if cond.condition is not None:
            statement = statement.where(cond.condition)

        statement = self.context.paginated_statement(statement, page, page_size)
        order_levels = [(dimension, hierarchy, levels)]
        statement = self.context.ordered_statement(statement, order,
                                                        order_levels)

        group_by = [self.context.column(attr) for attr in attributes]
        statement = statement.group_by(*group_by)

        if self.debug:
            self.logger.info("dimension values SQL:\n%s" % statement)

        result = self.connectable.execute(statement)
        labels = self.context.logical_labels(statement.columns)

        return ResultIterator(result, labels)

    def path_details(self, dimension, path, hierarchy=None):
        """Returns details for `path` in `dimension`. Can be used for
        multi-dimensional "breadcrumbs" in a used interface.

        Number of SQL queries: 1.
        """

        statement = self.context.detail_statement(dimension, path, hierarchy)
        labels = self.context.logical_labels(statement.columns)

        if self.debug:
            self.logger.info("path details SQL:\n%s" % statement)

        cursor = self.connectable.execute(statement)
        row = cursor.fetchone()

        if row:
            record = dict(zip(labels, row))
        else:
            record = None

        cursor.close()

        return record


    def aggregate(self, cell=None, measures=None, drilldown=None, split=None,
                  attributes=None, page=None, page_size=None, order=None,
                  include_summary=None, include_cell_count=None, **options):
        """Return aggregated result.

        Arguments:

        * `cell`: cell to be aggregated
        * `measures`: list of measures to be considered in aggregation
        * `drilldown`: list of dimensions or list of tuples: (`dimension`,
          `hierarchy`, `level`)
        * `split`: an optional cell that becomes an extra drilldown segmenting
          the data into those within split cell and those not within 
        * `attributes`: list of attributes from drilled-down dimensions to be
          returned in the result

        Query tuning:

        * `include_cell_count`: if ``True`` (``True`` is default) then
          `result.total_cell_count` is
          computed as well, otherwise it will be ``None``.
        * `include_summary`: if ``True`` (default) then summary is computed,
          otherwise it will be ``None``

        Result is paginated by `page_size` and ordered by `order`.

        Number of database queries:

        * without drill-down: 1 – summary
        * with drill-down (default): 3 – summary, drilldown, total drill-down
          record count

        Notes:

        * measures can be only in the fact table

        """

        if not cell:
            cell = Cell(self.cube)

        # TODO: add documentation

        # Coalesce measures - make sure that they are Attribute objects, not
        # strings. Strings are converted to corresponding Cube measure
        # attributes
        measures = [ self.cube.measure(measure) for measure in (measures if measures else self.cube.measures) ]

        result = AggregationResult(cell=cell, measures=measures)

        if include_summary or \
                ( include_summary is None and self.include_summary ) or \
                not drilldown:
            summary_statement = self.context.aggregation_statement(cell=cell,
                                                         measures=measures)

            if self.debug:
                self.logger.info("aggregation SQL:\n%s" % summary_statement)

            cursor = self.connectable.execute(summary_statement)
            row = cursor.fetchone()

            if row:
                # Convert SQLAlchemy object into a dictionary
                labels = self.context.logical_labels(summary_statement.columns)
                record = dict(zip(labels, row))
            else:
                record = None

            cursor.close()
            result.summary = record

        ##
        # Drill-down
        #
        # Note that a split cell if present prepends a drilldown
        ##

        if drilldown or split:
            drilldown = (levels_from_drilldown(cell, drilldown) if drilldown else [])

           
            dim_levels = {}
            for dim, hier, levels in drilldown:
                if dim.info.get('high_cardinality') and not (page_size and page is not None):
                    raise BrowserError("Cannot drilldown on high-cardinality dimension (%s) without including both page_size and page arguments" % (dim.name))
                if [ l for l in levels if l.info.get('high_cardinality') ] and not (page_size and page is not None):
                    raise BrowserError(("Cannot drilldown on high-cardinality levels (%s) " +
                                       "without including both page_size and page arguments") 
                                       % (",".join([l.key.ref() for l in levels if l.info.get('high_cardinality')])))

                dim_levels[str(dim)] = [str(level) for level in levels]

            if split:
                dim_levels[SPLIT_DIMENSION_NAME] = split.to_dict().get('cuts')

            result.levels = dim_levels

            statement = self.context.aggregation_statement(cell=cell,
                                                         measures=measures,
                                                         attributes=attributes,
                                                         drilldown=drilldown,
                                                         split=split)

            statement = self.context.paginated_statement(statement, page, page_size)
            statement = self.context.ordered_statement(statement, order,
                                                                    drilldown, split)

            if self.debug:
                self.logger.info("aggregation drilldown SQL:\n%s" % statement)

            dd_result = self.connectable.execute(statement)
            labels = self.context.logical_labels(statement.columns)

            # decorate with calculated measures if applicable
            calc_aggs = []
            for c in [ self.calculated_aggregations_for_measure(measure, drilldown, split) for measure in measures ]:
                calc_aggs += c
            result.calculators = calc_aggs
            result.cells = ResultIterator(dd_result, labels)


            # TODO: introduce option to disable this

            if include_cell_count or include_cell_count is None and self.include_cell_count:
                count_statement = statement.alias().count()
                row_count = self.connectable.execute(count_statement).fetchone()
                total_cell_count = row_count[0]
                result.total_cell_count = total_cell_count

        elif result.summary is not None:
            # do calculated measures on summary if no drilldown or split
            for calc_aggs in [ self.calculated_aggregations_for_measure(measure, drilldown, split) for measure in measures ]:
                for calc in calc_aggs:
                    calc(result.summary)

        return result

    def calculated_aggregations_for_measure(self, measure, drilldown_levels=None, split=None):
        """Returns a list of calculator objects that implement aggregations by calculating
        on retrieved results, given a particular drilldown.
        """
        if not measure.aggregations:
            return []

        # Each calculated aggregation calculates on every non-calculated aggregation.
        non_calculated_aggs = [ agg for agg in measure.aggregations if aggregation_functions.get(agg) is not None ]

        if not non_calculated_aggs:
            return []

        return [ func(measure, drilldown_levels, split, non_calculated_aggs) for func in filter(lambda f: f is not None, [ calculated_aggregation_functions.get(a) for a in measure.aggregations]) ]

    def validate(self):
        """Validate physical representation of model. Returns a list of
        dictionaries with keys: ``type``, ``issue``, ``object``.

        Types might be: ``join`` or ``attribute``.

        The ``join`` issues are:

        * ``no_table`` - there is no table for join
        * ``duplicity`` - either table or alias is specified more than once

        The ``attribute`` issues are:

        * ``no_table`` - there is no table for attribute
        * ``no_column`` - there is no column for attribute
        * ``duplicity`` - attribute is found more than once

        """
        issues = []

        # Check joins

        tables = set()
        aliases = set()
        alias_map = {}
        # 
        for join in self.mapper.joins:
            self.logger.debug("join: %s" % (join, ))

            if not join.master.column:
                issues.append(("join", "master column not specified", join))
            if not join.detail.table:
                issues.append(("join", "detail table not specified", join))
            elif join.detail.table == self.mapper.fact_name:
                issues.append(("join", "detail table should not be fact table", join))

            master_table = (join.master.schema, join.master.table)
            tables.add(master_table)

            detail_alias = (join.detail.schema, join.alias or join.detail.table)

            if detail_alias in aliases:
                issues.append(("join", "duplicate detail table %s" % detail_table, join))
            else:
                aliases.add(detail_alias)

            detail_table = (join.detail.schema, join.detail.table)
            alias_map[detail_alias] = detail_table

            if detail_table in tables and not join.alias:
                issues.append(("join", "duplicate detail table %s (no alias specified)" % detail_table, join))
            else:
                tables.add(detail_table)

        # Check for existence of joined tables:
        physical_tables = {}

        # Add fact table to support simple attributes
        physical_tables[(self.context.fact_table.schema, self.context.fact_table.name)] = self.context.fact_table
        for table in tables:
            try:
                physical_table = sqlalchemy.Table(table[1], self.metadata,
                                        autoload=True,
                                        schema=table[0] or self.mapper.schema)
                physical_tables[(table[0] or self.mapper.schema, table[1])] = physical_table
            except sqlalchemy.exc.NoSuchTableError:
                issues.append(("join", "table %s.%s does not exist" % table, join))

        # Check attributes

        attributes = self.mapper.all_attributes()
        physical = self.mapper.map_attributes(attributes)

        for attr, ref in zip(attributes, physical):
            alias_ref = (ref.schema, ref.table)
            table_ref = alias_map.get(alias_ref, alias_ref)
            table = physical_tables.get(table_ref)

            if table is None:
                issues.append(("attribute", "table %s.%s does not exist for attribute %s" % (table_ref[0], table_ref[1], self.mapper.logical(attr)), attr))
            else:
                try:
                    c = table.c[ref.column]
                except KeyError:
                    issues.append(("attribute", "column %s.%s.%s does not exist for attribute %s" % (table_ref[0], table_ref[1], ref.column, self.mapper.logical(attr)), attr))

        return issues

"""A Condition representation. `attributes` - list of attributes involved in
the conditions, `conditions` - SQL conditions"""
Condition = collections.namedtuple("Condition",
                                    ["attributes", "condition"])

"""Aliased table information"""
AliasedTable = collections.namedtuple("AliasedTable",
                                    ["schema", "table", "alias"])

# FIXME: Remove/dissolve this class, it is just historical remnant
# NOTE: the class was meant to contain reusable code by different SQL
#       backends
class QueryContext(object):

    def __init__(self, cube, mapper, metadata, **options):
        """Object providing context for constructing queries. Puts together
        the mapper and physical structure. `mapper` - which is used for
        mapping logical to physical attributes and performing joins.
        `metadata` is a `sqlalchemy.MetaData` instance for getting physical
        table representations.

        Object attributes:

        * `fact_table` – the physical fact table - `sqlalchemy.Table` instance
        * `tables` – a dictionary where keys are table references (schema,
          table) or (shchema, alias) to real tables - `sqlalchemy.Table`
          instances

        .. note::

            To get results as a dictionary, you should ``zip()`` the returned
            rows after statement execution with:

                labels = [column.name for column in statement.columns]
                ...
                record = dict(zip(labels, row))

            This is little overhead for a workaround for SQLAlchemy behaviour
            in SQLite database. SQLite engine does not respect dots in column
            names which results in "duplicate column name" error.
        """
        super(QueryContext, self).__init__()

        self.logger = get_logger()

        self.cube = cube
        self.mapper = mapper
        self.schema = mapper.schema
        self.metadata = metadata

        # Prepare physical fact table - fetch from metadata
        #
        self.fact_key = self.cube.key or DEFAULT_KEY_FIELD
        self.fact_name = mapper.fact_name
        try:
            self.fact_table = sqlalchemy.Table(self.fact_name, self.metadata,
                                           autoload=True, schema=self.schema)
        except sqlalchemy.exc.NoSuchTableError:
            in_schema = " in schema '%s'" if self.schema else ""
            msg = "No such fact table '%s'%s." % (self.fact_name, in_schema)
            raise WorkspaceError(msg)

        self.tables = {
                    (self.schema, self.fact_name): self.fact_table
                }

        # Collect all tables and their aliases.
        #
        # table_aliases contains mapping between aliased table name and real
        # table name with alias:
        # 
        #       (schema, aliased_name) --> (schema, real_name, alias)
        #
        self.table_aliases = {
            (self.schema, self.fact_name): (self.schema, self.fact_name, None)
        }

        # Collect all table aliases from joins detail tables
        for join in self.mapper.joins:
            # just ask for the table
            table = AliasedTable(join.detail.schema,
                                 join.detail.table,
                                 join.alias)
            table_alias = (join.detail.schema, join.alias or join.detail.table)
            self.table_aliases[table_alias] = table

        # Mapping where keys are attributes and values are columns
        self.logical_to_column = {}
        # Mapping where keys are column labels and values are attributes
        self.column_to_logical = {}

        self.safe_labels = options.get("safe_labels", False)
        self.label_counter = 1

    def aggregation_statement(self, cell, measures=None,
                              attributes=None, drilldown=None, split=None):
        """Return a statement for summarized aggregation. `whereclause` is
        same as SQLAlchemy `whereclause` for
        `sqlalchemy.sql.expression.select()`. `attributes` is list of logical
        references to attributes to be selected. If it is ``None`` then all
        attributes are used. `drilldown` has to be a dictionary. Use
        `levels_from_drilldown()` to prepare correct drill-down statement."""

        cell_cond = self.condition_for_cell(cell)
        split_dim_cond = None
        if split:
            split_dim_cond = self.condition_for_cell(split)

        if not attributes:
            attributes = set()

            if drilldown:
                for dim, hier, levels in drilldown:
                    for level in levels:
                        attributes |= set(level.attributes)

        attributes = set(attributes) | set(cell_cond.attributes)
        if split_dim_cond:
            attributes |= set(split_dim_cond.attributes)

        join_expression = self.join_expression_for_attributes(attributes)

        selection = []

        group_by = None
        drilldown_ptd_condition = None
        if split_dim_cond or drilldown:
            group_by = []
            if split_dim_cond:
                group_by.append(sql.expression.case([(split_dim_cond.condition, True)], else_=False).label(SPLIT_DIMENSION_NAME))
                selection.append(sql.expression.case([(split_dim_cond.condition, True)], else_=False).label(SPLIT_DIMENSION_NAME))
            for dim, hier, levels in drilldown:
                last_level = levels[-1] if len(levels) else None
                for level in levels:
                    columns = [self.column(attr) for attr in level.attributes
                                                        if attr in attributes]
                    group_by.extend(columns)
                    selection.extend(columns)
                    if last_level == level:
                        drilldown_ptd_condition = self.condition_for_level(level) or drilldown_ptd_condition

        # Measures
        if measures is None:
             measures = self.cube.measures

        # Collect "columns" for measure aggregations
        for measure in measures:
            selection.extend(self.aggregations_for_measure(measure))

        select = sql.expression.select(selection,
                                    from_obj=join_expression,
                                    use_labels=True,
                                    group_by=group_by)

        conditions = []
        if cell_cond.condition is not None:
            conditions.append(cell_cond.condition)
        if drilldown_ptd_condition is not None:
            conditions.append(drilldown_ptd_condition.condition)

        if conditions:
            select = select.where(sql.expression.and_(*conditions) if len(conditions) > 1 else conditions[0])

        return select

    def aggregations_for_measure(self, measure):
        """Returns list of aggregation functions (sqlalchemy) on measure
        columns.  The result columns are labeled as `measure` + ``_`` =
        `aggregation`, for example: ``amount_sum`` or ``discount_min``.

        `measure` has to be `Attribute` instance.

        If measure has no explicit aggregations associated, then ``sum`` is
        assumed.
        """

        if not measure.aggregations:
            aggregations = ["sum"]
        else:
            aggregations = [agg.lower() for agg in measure.aggregations]

        result = []
        for agg_name in aggregations:
            if not agg_name in aggregation_functions:
                if not agg_name in calculated_aggregation_functions:
                    raise ArgumentError("Unknown aggregation type %s for measure %s" % \
                                        (agg_name, measure))

            else:
                func = aggregation_functions[agg_name]
                label = "%s%s" % (str(measure), ("_" + agg_name if agg_name != "identity" else "") )
                aggregation = func(self.column(measure)).label(label)
                result.append(aggregation)

        return result

    def denormalized_statement(self, attributes=None, expand_locales=False,
                               include_fact_key=True, condition_attributes=None):
        """Return a statement (see class description for more information) for
        denormalized view. `whereclause` is same as SQLAlchemy `whereclause`
        for `sqlalchemy.sql.expression.select()`. `attributes` is list of
        logical references to attributes to be selected. If it is ``None``
        then all attributes are used. `condition_attributes` contains list of
        attributes that are not going to be selected, but are required for
        WHERE condition.

        Set `expand_locales` to ``True`` to expand all localized attributes.
        """

        if attributes is None:
            attributes = self.mapper.all_attributes()

        if condition_attributes:
            join_attributes = set(attributes) | condition_attributes
        else:
            join_attributes = set(attributes)

        join_expression = self.join_expression_for_attributes(join_attributes,
                                                expand_locales=expand_locales)

        columns = self.columns(attributes, expand_locales=expand_locales)

        if include_fact_key:
            key_column = self.fact_table.c[self.fact_key].label(self.fact_key)
            columns.insert(0, key_column)

        select = sql.expression.select(columns,
                                       from_obj=join_expression,
                                       use_labels=True)

        return select

    def detail_statement(self, dimension, path, hierarchy=None):
        """Returns statement for dimension details. `attributes` should be a
        list of attributes from one dimension that is one branch
        (master-detail) of a star/snowflake."""

        dimension = self.cube.dimension(dimension)
        hierarchy = dimension.hierarchy(hierarchy)
        attributes = hierarchy.all_attributes()

        expression = self.join_expression_for_attributes(attributes,
                                                        include_fact=False)
        columns = self.columns(attributes)
        select = sql.expression.select(columns,
                                       from_obj=expression,
                                       use_labels=True)

        cond = self.condition_for_point(dimension, path, hierarchy)
        select = select.where(cond.condition)

        self.logger.debug("\n\nSQL:\n%s \n\n" % select)

        return select

    def fact_statement(self, key_value):
        """Return a statement for selecting a single fact based on `key_value`"""

        key_column = self.fact_table.c[self.fact_key]
        condition = key_column == key_value

        statement = self.denormalized_statement()
        statement = statement.where(condition)

        return statement


    def join_expression_for_attributes(self, attributes, expand_locales=False,
                                        include_fact=True):
        """Returns a join expression for `attributes`"""
        physical_references = self.mapper.map_attributes(attributes, expand_locales=expand_locales)

        joins = self.mapper.relevant_joins(physical_references)
        return self.join_expression(joins, include_fact)

    def join_expression(self, joins, include_fact=True):
        """Create partial expression on a fact table with `joins` that can be
        used as core for a SELECT statement. `join` is a list of joins
        returned from mapper (most probably by `Mapper.relevant_joins()`)

        If `include_fact` is ``True`` (default) then fact table is considered
        as starting point. If it is ``False`` The first detail table is
        considered as starting point for joins. This might be useful when
        getting values of a dimension without cell restrictions.
        """

        self.logger.debug("create basic expression with %d joins" % len(joins))

        if include_fact:
            self.logger.debug("join: starting with fact table")
            expression = self.fact_table
        else:
            self.logger.debug("join: ignoring fact table")
            expression = None

        for join in joins:

            if not join.detail.table or (join.detail.table == self.fact_name and not join.alias):
                raise MappingError("Detail table name should be present and "
                                   "should not be a fact table unless aliased.")

            master_table = self.table(join.master.schema, join.master.table)
            detail_table = self.table(join.detail.schema, join.alias or join.detail.table)

            try:
                master_column = master_table.c[join.master.column]
            except:
                raise MappingError('Unable to find master key (schema %s) "%s"."%s" ' \
                                    % join.master[0:3])
            try:
                detail_column = detail_table.c[join.detail.column]
            except:
                raise MappingError('Unable to find detail key (schema %s) "%s"."%s" ' \
                                    % join.detail[0:3])

            onclause = master_column == detail_column

            if expression is not None:
                expression = sql.expression.join(expression, 
                                                 detail_table, 
                                                 onclause=onclause,
                                                 isouter=(include_fact and join.outer))
            else:
                self.logger.debug("join: starting with detail table '%s'" %
                                                                detail_table)
                expression = detail_table

        return expression

    def condition_for_cell(self, cell):
        """Constructs conditions for all cuts in the `cell`. Returns a named
        tuple with keys:

        * ``condition`` - SQL conditions
        * ``attributes`` - attributes that are involved in the conditions.
          This should be used for join construction.
        * ``group_by`` - attributes used for GROUP BY expression
        """

        if not cell:
            return Condition([], None)

        attributes = set()
        conditions = []

        for cut in cell.cuts:
            dim = self.cube.dimension(cut.dimension)

            if isinstance(cut, PointCut):
                path = cut.path
                wrapped_cond = self.condition_for_point(dim, path,
                                                        cut.hierarchy, cut.invert)

                condition = wrapped_cond.condition
                attributes |= wrapped_cond.attributes

            elif isinstance(cut, SetCut):
                set_conds = []

                for path in cut.paths:
                    wrapped_cond = self.condition_for_point(dim, path,
                                                            cut.hierarchy, False)
                    set_conds.append(wrapped_cond.condition)
                    attributes |= wrapped_cond.attributes

                condition = sql.expression.or_(*set_conds)
                if cut.invert:
                    condition = sql.expression.not_(condition)

            elif isinstance(cut, RangeCut):
                range_cond = self.range_condition(cut.dimension,
                                                  cut.hierarchy,
                                                  cut.from_path,
                                                  cut.to_path, cut.invert)
                condition = range_cond.condition
                attributes |= range_cond.attributes

            else:
                raise ArgumentError("Unknown cut type %s" % type(cut))

            conditions.append(condition)

        if conditions:
            condition = sql.expression.and_(*conditions)
        else:
            condition = None

        return Condition(attributes, condition)

    def condition_for_level(self, level):

        ref = self.mapper.physical(level.key)

        if not ref.condition:
            return None

        table = self.table(ref.schema, ref.table)
        try:
            column = table.c[ref.column]
        except:
            raise BrowserError("Unknown column '%s' in table '%s'" % (ref.column, ref.table))

        # evaluate the condition expression
        expr_func = eval(compile(ref.condition, '__expr__', 'eval'), _EXPR_EVAL_NS.copy())
        if not callable(expr_func):
            raise BrowserError("Cannot evaluate a callable object from reference's condition expr: %r" % ref)
        condition = expr_func(column)

        return Condition(set(), condition)

    def condition_for_point(self, dim, path, hierarchy=None, invert=False):
        """Returns a `Condition` tuple (`attributes`, `conditions`,
        `group_by`) dimension `dim` point at `path`. It is a compound
        condition - one equality condition for each path element in form:
        ``level[i].key = path[i]``"""

        attributes = set()
        conditions = []

        levels = dim.hierarchy(hierarchy).levels_for_path(path)

        if len(path) > len(levels):
            raise ArgumentError("Path has more items (%d: %s) than there are levels (%d) "
                                "in dimension %s" % (len(path), path, len(levels), dim.name))

        level_condition = None

        last_level = levels[-1] if len(levels) else None

        for level, value in zip(levels, path):

            # Prepare condition: dimension.level_key = path_value
            column = self.column(level.key)
            conditions.append(column == value)

            # only the lowermost level's condition should apply
            if level == last_level:
                level_condition = self.condition_for_level(level) or level_condition

            # FIXME: join attributes only if details are requested
            # Collect grouping columns
            for attr in level.attributes:
                attributes.add(attr)

        if level_condition:
            conditions.append(level_condition.condition)
            attributes = attributes | level_condition.attributes

        condition = sql.expression.and_(*conditions)

        if invert:
            condition = sql.expression.not_(condition)

        return Condition(attributes,condition)

    def range_condition(self, dim, hierarchy, from_path, to_path, invert=False):
        """Return a condition for a hierarchical range (`from_path`,
        `to_path`). Return value is a `Condition` tuple."""

        dim = self.cube.dimension(dim)

        lower, lower_ptd = self._boundary_condition(dim, hierarchy, from_path, 0)
        upper, upper_ptd = self._boundary_condition(dim, hierarchy, to_path, 1)

        ptd_condition = lower_ptd or upper_ptd

        conditions = []
        attributes = set()
        if lower.condition is not None:
            conditions.append(lower.condition)
            attributes |= lower.attributes
        if upper.condition is not None:
            conditions.append(upper.condition)
            attributes |= upper.attributes
        
        if ptd_condition and ptd_condition.condition is not None:
            conditions.append(ptd_condition.condition)
            attributes |= ptd_condition.attributes

        condexpr = sql.expression.and_(*conditions) if len(conditions) > 1 else conditions[0]

        if invert:
            condexpr = sql.expression.not_(condexpr)

        return Condition(attributes, condexpr)

    def _boundary_condition(self, dim, hierarchy, path, bound, first=True):
        """Return a `Condition` tuple for a boundary condition. If `bound` is
        1 then path is considered to be upper bound (operators < and <= are
        used), otherwise path is considered as lower bound (operators > and >=
        are used )"""

        if not path:
            return (Condition(set(), None), None)

        last, ptd_condition = self._boundary_condition(dim, hierarchy, path[:-1], bound, first=False)

        levels = dim.hierarchy(hierarchy).levels_for_path(path)

        if len(path) > len(levels):
            raise ArgumentError("Path has more items (%d: %s) than there are levels (%d) "
                                "in dimension %s" % (len(path), path, len(levels), dim.name))

        attributes = set()
        conditions = []

        last_level = levels[-1] if len(levels) else None

        for level, value in zip(levels[:-1], path[:-1]):
            column = self.column(level.key)
            conditions.append(column == value)

            if first and last_level == level:
                ptd_condition = self.condition_for_level(level) or ptd_condition

            for attr in level.attributes:
                attributes.add(attr)

        # Select required operator according to bound
        # 0 - lower bound
        # 1 - upper bound
        if bound == 1:
            # 1 - upper bound (that is <= and < operator)
            operator = sql.operators.le if first else sql.operators.lt
        else:
            # else - lower bound (that is >= and > operator)
            operator = sql.operators.ge if first else sql.operators.gt

        column = self.column(levels[-1].key)
        conditions.append( operator(column, path[-1]) )

        for attr in levels[-1].attributes:
            attributes.add(attr)

        condition = sql.expression.and_(*conditions)
        attributes |= last.attributes

        if last.condition is not None:
            condition = sql.expression.or_(condition, last.condition)
            attributes |= last.attributes

        return (Condition(attributes, condition), ptd_condition)

    def paginated_statement(self, statement, page, page_size):
        """Returns paginated statement if page is provided, otherwise returns
        the same statement."""

        if page is not None and page_size is not None:
            return statement.offset(page * page_size).limit(page_size)
        else:
            return statement

    def ordered_statement(self, statement, order, dimension_levels=None, split=None):
        """Returns a SQL statement which is ordered according to the `order`. If
        the statement contains attributes that have natural order specified, then
        the natural order is used, if not overriden in the `order`.

        `dimension_levels` is list of considered dimension levels in form of
        tuples (`dimension`, `levels`). For each level it's sort key is used.
        """

        # Each attribute mentioned in the order should be present in the selection
        # or as some column from joined table. Here we get the list of already
        # selected columns and derived aggregates

        selection = collections.OrderedDict()

        # Get logical attributes from column labels (see logical_labels method
        # description for more information why this step is necessary)
        logical = self.logical_labels(statement.columns)
        for column, ref in zip(statement.columns, logical):
            selection[ref] = column

        # Make sure that the `order` is a list of of tuples (`attribute`,
        # `order`). If element of the `order` list is a string, then it is
        # converted to (`string`, ``None``).

        order = order or []

        if split:
            order.append( SPLIT_DIMENSION_NAME )

        if dimension_levels:
            for dim, hier, levels in dimension_levels:
                dim = self.cube.dimension(dim)
                for level in levels:
                    level = dim.level(level)
                    if level.order:
                        order.append( (level.order_attribute.ref(), level.order) )

        order_by = collections.OrderedDict()

        for item in order:
            if item == SPLIT_DIMENSION_NAME:
                column = sql.expression.column(SPLIT_DIMENSION_NAME)
            elif isinstance(item, basestring):
                try:
                    column = selection[item]
                except KeyError:
                    attribute = self.mapper.attribute(item)
                    column = self.column(attribute)

            else:
                # item is a two-element tuple where first element is attribute
                # name and second element is ordering
                try:
                    column = selection[item[0]]
                except KeyError:
                    attribute = self.mapper.attribute(item[0])
                    column = self.column(attribute)

                column = order_column(column, item[1])

            if item not in order_by:
                order_by[item] = column

        # Collect natural order for selected columns
        for (name, column) in selection.items():
            try:
                # Backward mapping: get Attribute instance by name. The column
                # name used here is already labelled to the logical name
                attribute = self.mapper.attribute(name)
            except KeyError:
                # Since we are already selecting the column, then it should
                # exist this exception is raised when we are trying to get
                # Attribute object for an aggregate - we can safely ignore
                # this.

                # TODO: add natural ordering for measures (may be nice)
                attribute = None

            if attribute and attribute.order and name not in order_by.keys():
                order_by[name] = order_column(column, attribute.order)

        return statement.order_by(*order_by.values())

    def table(self, schema, table_name):
        """Return a SQLAlchemy Table instance. If table was already accessed,
        then existing table is returned. Otherwise new instance is created.

        If `schema` is ``None`` then browser's default schema is used.
        """

        aliased_ref = (schema or self.schema, table_name)

        if aliased_ref in self.tables:
            return self.tables[aliased_ref]

        # Get real table reference
        try:
            table_ref = self.table_aliases[aliased_ref]
        except KeyError:
            raise ModelError("Table with reference %s not found. "
                             "Missing join in cube '%s'?" %
                                    (aliased_ref, self.cube.name) )

        table = sqlalchemy.Table(table_ref.table, self.metadata,
                                 autoload=True, schema=table_ref.schema)

        self.logger.debug("registering table '%s' as '%s'" % (table_ref.table,
                                                                table_name))
        if table_ref.alias:
            table = table.alias(table_ref.alias)

        self.tables[aliased_ref] = table

        return table

    def column(self, attribute, locale=None):
        """Return a column object for attribute. `locale` is explicit locale
        to be used. If not specified, then the current browsing/mapping locale
        is used for localizable attributes."""

        logical = self.mapper.logical(attribute, locale)
        if logical in self.logical_to_column:
            return self.logical_to_column[logical]

        ref = self.mapper.physical(attribute, locale)
        table = self.table(ref.schema, ref.table)
        try:
            column = table.c[ref.column]
        except:
            # FIXME: do not expose this exception to server
            avail = [str(c) for c in table.columns]
            raise BrowserError("Unknown column '%s' in table '%s' avail: %s" %
                                        (ref.column, ref.table, avail))

        # Extract part of the date
        if ref.extract:
            column = sql.expression.extract(ref.extract, column)
        if ref.func:
            column = getattr(sql.expression.func, ref.func)(column)
        if ref.expr:
            expr_func = eval(compile(ref.expr, '__expr__', 'eval'), _EXPR_EVAL_NS.copy())
            if not callable(expr_func):
                raise BrowserError("Cannot evaluate a callable object from reference's expr: %r" % ref)
            column = expr_func(column)
        if self.safe_labels:
            label = "a%d" % self.label_counter
            self.label_counter += 1
        else:
            label = logical

        if isinstance(column, basestring):
            raise ValueError("Cannot resolve %s to a column object: %r" % (attribute, column))

        column = column.label(label)

        self.logical_to_column[logical] = column
        self.column_to_logical[label] = logical

        return column

    def columns(self, attributes, expand_locales=False):
        """Returns list of columns.If `expand_locales` is True, then one
        column per attribute locale is added."""

        if expand_locales:
            columns = []
            for attr in attributes:
                if attr.locales:
                    columns += [self.column(attr, locale) for locale in attr.locales]
                else: # if not attr.locales
                    columns.append(self.column(attr))
        else:
            columns = [self.column(attr) for attr in attributes]

        return columns

    def logical_labels(self, columns):
        """Returns list of logical attribute labels from list of columns
        or column labels.

        This method and additional internal references were added because some
        database dialects, such as Exasol, can not handle dots in column
        names, even when quoted.
        """

        attributes = []

        _QUOTE_STRIPPER = re.compile(r"^\"(.+)\"$")
        for column in columns:
            attributes.append(self.column_to_logical.get(column.name,
                                                         column.name))

        return attributes

class AggregatedCubeBrowser(AggregationBrowser):
    """docstring for SnowflakeBrowser"""

    def __init__(self, cube, connectable=None, locale=None, metadata=None,
                 debug=False, **options):
        """AggregatedCubeBrowser is a SQL-based AggregationBrowser
        implementation that uses pre-aggregated table.

        Attributes:

        * `cube` - browsed cube
        * `connectable` - SQLAlchemy connectable object (engine or connection)
        * `locale` - locale used for browsing
        * `metadata` - SQLAlchemy MetaData object
        * `debug` - output SQL to the logger at INFO level
        * `options` - passed to the mapper and context (see their respective
          documentation)

        """
        super(AggregatedCubeBrowser, self).__init__(cube)

        if cube == None:
            raise ArgumentError("Cube for browser should not be None.")

        self.logger = get_logger()

        self.cube = cube
        self.locale = locale or cube.model.locale
        self.debug = debug

        if connectable is not None:
            self.connectable = connectable
            self.metadata = metadata or sqlalchemy.MetaData(bind=self.connectable)

        # Mapper is responsible for finding corresponding physical columns to
        # dimension attributes and fact measures. It also provides information
        # about relevant joins to be able to retrieve certain attributes.

        if options.get("use_denormalization"):
            mapper_class = DenormalizedMapper
        else:
            mapper_class = SnowflakeMapper

        self.logger.debug("using mapper %s for cube '%s' (locale: %s)" % \
                            (str(mapper_class.__name__), cube.name, locale))

        self.mapper = mapper_class(cube, locale=self.locale, **options)
        self.logger.debug("mapper schema: %s" % self.mapper.schema)

        # QueryContext is creating SQL statements (using SQLAlchemy). It
        # also caches information about tables retrieved from metadata.

        self.context = QueryContext(self.cube, self.mapper,
                                      metadata=self.metadata)

        # Map: logical attribute --> 
        self.attribute_columns = {}
        self.alias_columns

def order_column(column, order):
    """Orders a `column` according to `order` specified as string."""

    if not order:
        return column
    elif order.lower().startswith("asc"):
        return column.asc()
    elif order.lower().startswith("desc"):
        return column.desc()
    else:
        raise ArgumentError("Unknown order %s for column %s") % (order, column)


class ResultIterator(object):
    """
    Iterator that returns SQLAlchemy ResultProxy rows as dictionaries
    """
    def __init__(self, result, labels):
        self.result = result
        self.batch = None
        self.labels = labels

    def __iter__(self):
        return self

    def next(self):
        if not self.batch:
            many = self.result.fetchmany()
            if not many:
                raise StopIteration
            self.batch = collections.deque(many)

        row = self.batch.popleft()

        return dict(zip(self.labels, row))

