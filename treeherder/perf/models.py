import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator
from django.db import models
from django.utils.timezone import now as django_now

from treeherder.model.models import (Job,
                                     MachinePlatform,
                                     OptionCollection,
                                     Push,
                                     Repository)
from treeherder.perf.exceptions import (MaxRuntimeExceeded,
                                        NoDataCyclingAtAll)

SIGNATURE_HASH_LENGTH = 40


class PerformanceFramework(models.Model):

    name = models.SlugField(max_length=255, unique=True)
    enabled = models.BooleanField(default=False)

    class Meta:
        db_table = 'performance_framework'

    def __str__(self):
        return self.name


class PerformanceSignature(models.Model):

    signature_hash = models.CharField(max_length=SIGNATURE_HASH_LENGTH,
                                      validators=[
                                          MinLengthValidator(SIGNATURE_HASH_LENGTH)
                                      ])

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    framework = models.ForeignKey(PerformanceFramework, on_delete=models.CASCADE)
    platform = models.ForeignKey(MachinePlatform, on_delete=models.CASCADE)
    option_collection = models.ForeignKey(OptionCollection, on_delete=models.CASCADE)
    suite = models.CharField(max_length=80)
    test = models.CharField(max_length=80, blank=True)
    lower_is_better = models.BooleanField(default=True)
    last_updated = models.DateTimeField(db_index=True)
    parent_signature = models.ForeignKey('self', on_delete=models.CASCADE, related_name='subtests',
                                         null=True, blank=True)
    has_subtests = models.BooleanField()

    # extra options to distinguish the test (that don't fit into
    # option collection for whatever reason)
    # generous max_length permits up to 8 verbose option names
    extra_options = models.CharField(max_length=360, blank=True)

    # TODO: reduce length to minimum value
    # TODO: make this nonnullable, once we demand
    #  all PERFHERDER_DATA dumps to provide the unit field
    measurement_unit = models.CharField(max_length=50, null=True)

    # these properties override the default settings for how alert
    # generation works
    ALERT_PCT = 0
    ALERT_ABS = 1
    ALERT_CHANGE_TYPES = ((ALERT_PCT, 'percentage'),
                          (ALERT_ABS, 'absolute'))

    should_alert = models.NullBooleanField()
    alert_change_type = models.IntegerField(choices=ALERT_CHANGE_TYPES,
                                            null=True)
    alert_threshold = models.FloatField(null=True)
    min_back_window = models.IntegerField(null=True)
    max_back_window = models.IntegerField(null=True)
    fore_window = models.IntegerField(null=True)

    @staticmethod
    def _get_alert_change_type(alert_change_type_input):
        '''
        Maps a schema-specified alert change type to the internal index
        value
        '''
        for (idx, enum_val) in PerformanceSignature.ALERT_CHANGE_TYPES:
            if enum_val == alert_change_type_input:
                return idx
        return None

    class Meta:
        db_table = 'performance_signature'

        unique_together = (
            # ensure there is only one signature per repository with a
            # particular set of properties
            ('repository', 'framework', 'platform', 'option_collection',
             'suite', 'test', 'last_updated', 'extra_options'),
            # ensure there is only one signature of any hash per
            # repository (same hash in different repositories is allowed)
            ('repository', 'framework', 'signature_hash'),
        )

    def __str__(self):
        name = self.suite
        if self.test:
            name += " {}".format(self.test)
        else:
            name += " summary"

        return "{} {} {} {}".format(self.signature_hash, name, self.platform,
                                    self.last_updated)


class PerformanceDatumManager(models.Manager):
    """
    Convenience functions for operations on groups of performance datums
    """

    @classmethod
    def _maybe_quit(cls, started_at, max_overall_runtime):
        now = datetime.datetime.now()
        elapsed_runtime = now - started_at

        if max_overall_runtime < elapsed_runtime:
            raise MaxRuntimeExceeded('Max runtime for performance data cycling exceeded')

    def cycle_data(self, cycle_interval, chunk_size,
                   logger, started_at, max_overall_runtime):
        """Delete data older than cycle_interval, splitting the target data
into chunks of chunk_size size."""
        max_timestamp = datetime.datetime.now() - cycle_interval

        try:
            self._delete_in_chunks(max_timestamp, chunk_size, logger,
                                   started_at, max_overall_runtime)

            # also remove any signatures which are (no longer) associated with
            # a job
            logger.warning('Removing performance signatures with missing jobs...')
            for signature in PerformanceSignature.objects.all():
                self._maybe_quit(started_at, max_overall_runtime)

                if not self.filter(
                        repository_id=signature.repository_id,  # leverages (repository, signature) compound index
                        signature_id=signature.id).exists():
                    signature.delete()
        except NoDataCyclingAtAll as ex:
            logger.warning('Exception: {}'.format(ex))
        except MaxRuntimeExceeded as ex:
            logger.warning(ex)

    def _delete_in_chunks(self, max_timestamp, chunk_size, logger,
                          started_at, max_overall_runtime):
        from django.db import connection
        any_succesful_attempt = False

        with connection.cursor() as cursor:
            while True:
                self._maybe_quit(started_at, max_overall_runtime)

                try:
                    ideal_chunk_size = self._compute_ideal_chunk_size(max_timestamp, chunk_size)
                    cursor.execute('''
                        DELETE FROM `performance_datum`
                        WHERE push_timestamp < %s
                        LIMIT %s
                    ''', [max_timestamp, ideal_chunk_size])
                except Exception as ex:
                    logger.warning('Failed to delete performance data chunk, while running "{}" query '
                                   .format(cursor._last_executed))

                    if any_succesful_attempt is False:
                        raise NoDataCyclingAtAll from ex
                    break
                else:
                    deleted_rows = cursor.rowcount

                    if deleted_rows == 0:
                        break  # finished removing all expired data
                    else:
                        any_succesful_attempt = True
                        logger.warning('Successfully deleted {} performance datum rows'.format(deleted_rows))

    def _compute_ideal_chunk_size(self, max_timestamp, max_chunk_size):
        '''
        max_chunk_size may be too big, causing
        timeouts while attempting deletion;
        doing basic database query, maybe a lower value is better
        '''
        max_id = self.filter(push_timestamp__gt=max_timestamp
                             ).order_by('-id')[0].id
        older_ids = self.filter(push_timestamp__lte=max_timestamp, id__lte=max_id
                                ).order_by('id')[:max_chunk_size]

        return len(older_ids) or max_chunk_size


class PerformanceDatum(models.Model):

    objects = PerformanceDatumManager()

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    signature = models.ForeignKey(PerformanceSignature, on_delete=models.CASCADE)
    value = models.FloatField()
    push_timestamp = models.DateTimeField(db_index=True)

    # job information can expire before the performance datum
    job = models.ForeignKey(Job, null=True, default=None,
                            on_delete=models.SET_NULL)
    push = models.ForeignKey(Push, on_delete=models.CASCADE)

    # the following properties are obsolete and should be removed at some
    # point
    ds_job_id = models.PositiveIntegerField(db_column="ds_job_id", null=True)
    result_set_id = models.PositiveIntegerField(null=True)

    class Meta:
        db_table = 'performance_datum'
        index_together = [
            # Speeds up the typical "get a range of performance datums" query
            ('repository', 'signature', 'push_timestamp'),
            # Speeds up the compare view in treeherder (we only index on
            # repository because we currently filter on it in the query)
            ('repository', 'signature', 'push')]
        unique_together = ('repository', 'job', 'push', 'signature')

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)  # Call the "real" save() method.
        if self.signature.last_updated < self.push_timestamp:
            self.signature.last_updated = self.push_timestamp
            self.signature.save()

    def __str__(self):
        return "{} {}".format(self.value, self.push_timestamp)


class IssueTracker(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, blank=False)
    task_base_url = models.URLField(max_length=512, null=False)

    class Meta:
        db_table = "issue_tracker"

    def __str__(self):
        return "{} (tasks via {})".format(self.name, self.task_base_url)


class PerformanceAlertSummary(models.Model):
    '''
    A summarization of performance alerts

    A summary of "alerts" that the performance numbers for a specific
    repository have changed at a particular time.

    See also the :ref:`PerformanceAlert` class below.
    '''
    id = models.AutoField(primary_key=True)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    framework = models.ForeignKey(PerformanceFramework, on_delete=models.CASCADE)

    prev_push = models.ForeignKey(Push, on_delete=models.CASCADE, related_name='+')
    push = models.ForeignKey(Push, on_delete=models.CASCADE, related_name='+')

    manually_created = models.BooleanField(default=False)

    notes = models.TextField(null=True, blank=True)
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                 related_name='assigned_alerts')

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    first_triaged = models.DateTimeField(null=True, default=None)
    last_updated = models.DateTimeField(auto_now=True, null=True)

    UNTRIAGED = 0
    DOWNSTREAM = 1
    REASSIGNED = 2
    INVALID = 3
    IMPROVEMENT = 4
    INVESTIGATING = 5
    WONTFIX = 6
    FIXED = 7
    BACKED_OUT = 8
    CONFIRMING = 9

    STATUSES = ((UNTRIAGED, 'Untriaged'),
                (DOWNSTREAM, 'Downstream'),
                (REASSIGNED, 'Reassigned'),
                (INVALID, 'Invalid'),
                (IMPROVEMENT, 'Improvement'),
                (INVESTIGATING, 'Investigating'),
                (WONTFIX, 'Won\'t fix'),
                (FIXED, 'Fixed'),
                (BACKED_OUT, 'Backed out'),
                (CONFIRMING, 'Confirming'))

    status = models.IntegerField(choices=STATUSES, default=UNTRIAGED)

    bug_number = models.PositiveIntegerField(null=True)
    bug_updated = models.DateTimeField(null=True)

    issue_tracker = models.ForeignKey(IssueTracker,
                                      on_delete=models.PROTECT,
                                      default=1)  # Bugzilla

    def __init__(self, *args, **kwargs):
        super(PerformanceAlertSummary, self).__init__(*args, **kwargs)

        # allows updating timestamps only on new values
        self.__prev_bug_number = self.bug_number

    def save(self, *args, **kwargs):
        if (self.bug_number is not None and
                self.bug_number != self.__prev_bug_number):
            self.bug_updated = datetime.datetime.now()
        super(PerformanceAlertSummary, self).save(*args, **kwargs)
        self.__prev_bug_number = self.bug_number

    def update_status(self, using=None):
        autodetermined_status = self.autodetermine_status()
        if autodetermined_status != self.status:
            self.status = autodetermined_status
            self.save(using=using)

    def autodetermine_status(self):
        alerts = (PerformanceAlert.objects.filter(summary=self) | PerformanceAlert.objects.filter(related_summary=self))

        # if no alerts yet, we'll say untriaged
        if not alerts:
            return PerformanceAlertSummary.UNTRIAGED

        # if any untriaged, then set to untriaged
        if any(alert.status == PerformanceAlert.UNTRIAGED for alert in alerts):
            return PerformanceAlertSummary.UNTRIAGED

        if any(alert.status == PerformanceAlert.CONFIRMING for alert in alerts):
            return PerformanceAlertSummary.CONFIRMING

        # if all invalid, then set to invalid
        if all(alert.status == PerformanceAlert.INVALID for alert in alerts):
            return PerformanceAlertSummary.INVALID

        # otherwise filter out invalid alerts
        alerts = [a for a in alerts if a.status != PerformanceAlert.INVALID]

        # if there are any "acknowledged" alerts, then set to investigating
        # if not one of the resolved statuses and there are regressions,
        # otherwise we'll say it's an improvement
        if any(alert.status == PerformanceAlert.ACKNOWLEDGED for alert in alerts):
            if all(not alert.is_regression for alert in
                   alerts if alert.status == PerformanceAlert.ACKNOWLEDGED):
                return PerformanceAlertSummary.IMPROVEMENT
            elif self.status not in (PerformanceAlertSummary.IMPROVEMENT,
                                     PerformanceAlertSummary.INVESTIGATING,
                                     PerformanceAlertSummary.WONTFIX,
                                     PerformanceAlertSummary.FIXED,
                                     PerformanceAlertSummary.BACKED_OUT):
                return PerformanceAlertSummary.INVESTIGATING
            # keep status if one of the investigating ones
            return self.status

        # at this point, we've determined that this is a summary with no valid
        # alerts of its own: all alerts should be either reassigned,
        # downstream, or invalid (but not all invalid, that case is covered
        # above)
        if any(alert.status == PerformanceAlert.REASSIGNED for alert in alerts):
            return PerformanceAlertSummary.REASSIGNED

        return PerformanceAlertSummary.DOWNSTREAM

    def timestamp_first_triage(self):
        # called for summary specific updates (e.g. notes, bug linking)
        if self.first_triaged is None:
            self.first_triaged = django_now()
        return self

    class Meta:
        db_table = "performance_alert_summary"
        unique_together = ('repository', 'framework', 'prev_push', 'push')

    def __str__(self):
        return "{} {} {}-{}".format(self.framework, self.repository,
                                    self.prev_push.revision,
                                    self.push.revision)


class PerformanceAlert(models.Model):
    '''
    A single performance alert

    An individual "alert" that the numbers in a specific performance
    series have consistently changed level at a specific time.

    An alert is always a member of an alert summary, which groups all
    the alerts associated with a particular push together. In many cases at
    Mozilla, the original alert summary is not correct, so we allow reassigning
    it to a different (revised) summary.
    '''
    id = models.AutoField(primary_key=True)
    summary = models.ForeignKey(PerformanceAlertSummary,
                                on_delete=models.CASCADE,
                                related_name='alerts')
    related_summary = models.ForeignKey(PerformanceAlertSummary,
                                        on_delete=models.CASCADE,
                                        related_name='related_alerts',
                                        null=True)
    series_signature = models.ForeignKey(PerformanceSignature, on_delete=models.CASCADE)
    is_regression = models.BooleanField()
    starred = models.BooleanField(default=False)
    classifier = models.ForeignKey(User, on_delete=models.CASCADE, null=True)  # null if autoclassified

    created = models.DateTimeField(auto_now_add=True, null=True)
    # time when human user 1st interacted with alert
    # IMPORTANT: workers & automated scripts mustn't update this! Dont' assign this outside setter method!
    first_triaged = models.DateTimeField(null=True, default=None)
    last_updated = models.DateTimeField(auto_now=True, null=True)

    UNTRIAGED = 0
    DOWNSTREAM = 1
    REASSIGNED = 2
    INVALID = 3
    ACKNOWLEDGED = 4
    CONFIRMING = 5

    # statuses where we relate this alert to another summary
    RELATIONAL_STATUS_IDS = (DOWNSTREAM, REASSIGNED)
    # statuses where this alert is related only to the summary it was
    # originally assigned to
    UNRELATIONAL_STATUS_IDS = (UNTRIAGED, INVALID, ACKNOWLEDGED, CONFIRMING)

    STATUSES = ((UNTRIAGED, 'Untriaged'),
                (DOWNSTREAM, 'Downstream'),
                (REASSIGNED, 'Reassigned'),
                (INVALID, 'Invalid'),
                (ACKNOWLEDGED, 'Acknowledged'),
                (CONFIRMING, 'Confirming'))

    status = models.IntegerField(choices=STATUSES, default=UNTRIAGED)

    amount_pct = models.FloatField(
        help_text="Amount in percentage that series has changed")
    amount_abs = models.FloatField(
        help_text="Absolute amount that series has changed")
    prev_value = models.FloatField(
        help_text="Previous value of series before change")
    new_value = models.FloatField(
        help_text="New value of series after change")
    t_value = models.FloatField(
        help_text="t value out of analysis indicating confidence "
        "that change is 'real'", null=True)

    manually_created = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        # validate that we set a status that makes sense for presence
        # or absence of a related summary
        if self.related_summary and self.status not in self.RELATIONAL_STATUS_IDS:
            raise ValidationError("Related summary set but status not in "
                                  "'{}'!".format(", ".join(
                                      [STATUS[1] for STATUS in self.STATUSES if
                                       STATUS[0] in self.RELATIONAL_STATUS_IDS])))
        if not self.related_summary and self.status not in self.UNRELATIONAL_STATUS_IDS:
            raise ValidationError("Related summary not set but status not in "
                                  "'{}'!".format(", ".join(
                                      [STATUS[1] for STATUS in self.STATUSES if
                                       STATUS[0] in self.UNRELATIONAL_STATUS_IDS])))

        super().save(*args, **kwargs)

        # check to see if we need to update the summary statuses

        # just forward the explicit database
        # so the summary properly updates there
        using = kwargs.get('using', None)
        self.summary.update_status(using=using)
        if self.related_summary:
            self.related_summary.update_status(using=using)

    def timestamp_first_triage(self):
        # use only on code triggered by
        # human interaction
        if self.first_triaged is None:
            self.first_triaged = django_now()
            self.summary.timestamp_first_triage().save()
        return self

    class Meta:
        db_table = "performance_alert"
        unique_together = ('summary', 'series_signature')

    def __str__(self):
        return "{} {} {}%".format(self.summary, self.series_signature,
                                  self.amount_pct)


class PerformanceBugTemplate(models.Model):
    '''
    Template for filing a bug or issue associated with a performance alert
    '''
    framework = models.OneToOneField(PerformanceFramework, on_delete=models.CASCADE)

    keywords = models.CharField(max_length=255)
    status_whiteboard = models.CharField(max_length=255)
    default_component = models.CharField(max_length=255)
    default_product = models.CharField(max_length=255)
    cc_list = models.CharField(max_length=255)

    text = models.TextField(max_length=4096)

    class Meta:
        db_table = "performance_bug_template"

    def __str__(self):
        return '{} bug template'.format(self.framework.name)
