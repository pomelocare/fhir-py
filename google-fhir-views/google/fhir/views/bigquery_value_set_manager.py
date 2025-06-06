# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Library for persisting value sets to BigQuery."""

from typing import Iterable, Optional, Union

from google.cloud import bigquery
import sqlalchemy
import sqlalchemy_bigquery

from proto.google.fhir.proto.r4.core.resources import value_set_pb2
from google.fhir.r4.terminology import terminology_service_client
from google.fhir.r4.terminology import value_set_tables
from google.fhir.r4.terminology import value_sets


class BigQueryValueSetManager:
  """Utility for managing value set in BigQuery."""

  def __init__(
      self,
      client: bigquery.client.Client,
      value_set_codes_table: Union[
          bigquery.table.Table, bigquery.table.TableReference, str
      ],
  ) -> None:
    """Initializes the BigQueryValueSetManager with user provided client.

    Args:
      client: BigQuery client with which to perform queries.
      value_set_codes_table: A table containing value set expansions. If
        `value_set_codes_table` is a string, it must included a project ID if
        not in the client's default project, dataset ID and table ID, each
        separated by a '.'. The table must match the schema described by
        https://github.com/FHIR/sql-on-fhir/blob/master/sql-on-fhir.md#valueset-support
    """
    super().__init__()
    self._client = client

    if isinstance(value_set_codes_table, str):
      self._value_set_codes_table = bigquery.table.TableReference.from_string(
          value_set_codes_table, default_project=client.project
      )
    else:
      self._value_set_codes_table = value_set_codes_table

  @property
  def value_set_codes_table(self) -> bigquery.table.TableReference:
    """The value set code table."""
    return self._value_set_codes_table

  def _create_valueset_codes_table_if_not_exists(self) -> bigquery.table.Table:
    """Creates a table for storing value set code mappings.

    Creates a table named after the `value_set_codes_table` provided at class
    initialization as described by
    https://github.com/FHIR/sql-on-fhir/blob/master/sql-on-fhir.md#valueset-support

    If the table already exists, no action is taken.

    Returns:
      An bigquery.Table object representing the created table.
    """
    schema = [
        bigquery.SchemaField('valueseturi', 'STRING', mode='REQUIRED'),
        bigquery.SchemaField('valuesetversion', 'STRING', mode='NULLABLE'),
        bigquery.SchemaField('system', 'STRING', mode='REQUIRED'),
        bigquery.SchemaField('code', 'STRING', mode='REQUIRED'),
    ]
    table = bigquery.Table(self._value_set_codes_table, schema=schema)
    table.clustering_fields = ['valueseturi', 'code']
    return self._client.create_table(table, exists_ok=True)

  # TODO(b/201107372): Update FHIR-agnostic types to a protocol.
  def materialize_value_sets(
      self,
      value_set_protos: Iterable[value_set_pb2.ValueSet],
      batch_size: int = 500,
  ) -> None:
    """Materialize the given value sets into the value_set_codes_table.

    Then writes these expanded codes into the database
    named after the `value_set_codes_table` provided at class initialization.
    Builds a valueset_codes table as described by
    https://github.com/FHIR/sql-on-fhir/blob/master/sql-on-fhir.md#valueset-support

    The table will be created if it does not already exist.

    The function will avoid inserting duplicate rows if some of the codes are
    already present in the given table. It will not attempt to perform an
    'upsert' or modify any existing rows.

    Note that value sets provided to this function should already be expanded,
    in that they contain the code values to write. Users should also see
    `materialize_value_set_expansion` below to retrieve an expanded set from
    a terminology server.

    Args:
      value_set_protos: An iterable of FHIR ValueSet protos.
      batch_size: The maximum number of rows to insert in a single query.
    """
    bq_table = self._create_valueset_codes_table_if_not_exists()

    sa_table = _bq_table_to_sqlalchemy_table(bq_table)
    queries = value_set_tables.valueset_codes_insert_statement_for(
        value_set_protos, sa_table, batch_size=batch_size
    )

    # Render the query objects as strings and use the client to execute them.
    for query in queries:
      query_string = str(
          query.compile(
              dialect=(sqlalchemy_bigquery.BigQueryDialect()),
              compile_kwargs={'literal_binds': True},
          )
      )
      self._client.query(query_string).result()

  def materialize_value_set_expansion(
      self,
      urls: Iterable[str],
      expander: Union[
          terminology_service_client.TerminologyServiceClient,
          value_sets.ValueSetResolver,
      ],
      terminology_service_url: Optional[str] = None,
      batch_size: int = 500,
  ) -> None:
    """Expands a sequence of value set and materializes their expanded codes.

    Expands the given value set URLs to obtain the set of codes they describe.
    Then writes these expanded codes into the database
    named after the `value_set_codes_table` provided at class initialization.
    Builds a valueset_codes table as described by
    https://github.com/FHIR/sql-on-fhir/blob/master/sql-on-fhir.md#valueset-support

    The table will be created if it does not already exist.

    The function will avoid inserting duplicate rows if some of the codes are
    already present in the given table. It will not attempt to perform an
    'upsert' or modify any existing rows.

    Provided as a utility function for user convenience. If `urls` is a large
    set of URLs, callers may prefer to use multi-processing and/or
    multi-threading to perform expansion and table insertion of the URLs
    concurrently. This function performs all expansions and table insertions
    serially.

    Args:
      urls: The urls for value sets to expand and materialize.
      expander: The ValueSetResolver or TerminologyServiceClient to perform
        value set expansion. A ValueSetResolver may be used to attempt to avoid
        some network requests by expanding value sets locally. A
        TerminologyServiceClient will use external terminology services to
        perform all value set expansions.
      terminology_service_url: If `expander` is a TerminologyServiceClient, the
        URL of the terminology service to use when expanding value set URLs. If
        not given, the client will attempt to infer the correct terminology
        service to use for each value set URL based on its domain.
      batch_size: The maximum number of rows to insert in a single query.

    Raises:
      TypeError: If a `terminology_service_url` is given but `expander` is not a
      TerminologyServiceClient.
    """
    if terminology_service_url is not None and not isinstance(
        expander, terminology_service_client.TerminologyServiceClient
    ):
      raise TypeError(
          '`terminology_service_url` can only be given if `expander` is a '
          'TerminologyServiceClient'
      )

    if terminology_service_url is not None and isinstance(
        expander, terminology_service_client.TerminologyServiceClient
    ):
      expanded_value_sets = (
          expander.expand_value_set_url_using_service(
              url, terminology_service_url
          )
          for url in urls
      )
    else:
      expanded_value_sets = (expander.expand_value_set_url(url) for url in urls)

    self.materialize_value_sets(expanded_value_sets, batch_size=batch_size)


def _bq_table_to_sqlalchemy_table(
    bq_table: bigquery.table.Table,
) -> sqlalchemy.sql.selectable.TableClause:
  """Converts a BigQuery client Table to an sqlalchemy Table."""
  table_name = f'{bq_table.project}.{bq_table.dataset_id}.{bq_table.table_id}'
  columns = [sqlalchemy.column(column.name) for column in bq_table.schema]
  return sqlalchemy.table(table_name, *columns)
