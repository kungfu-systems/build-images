package io.kungfu.aeron;

import io.aeron.Aeron;
import io.aeron.ChannelUri;
import io.aeron.ExclusivePublication;
import io.aeron.Image;
import io.aeron.Publication;
import io.aeron.Subscription;
import io.aeron.archive.Archive;
import io.aeron.archive.ArchiveThreadingMode;
import io.aeron.archive.ArchivingMediaDriver;
import io.aeron.archive.client.AeronArchive;
import io.aeron.archive.codecs.SourceLocation;
import io.aeron.archive.status.RecordingPos;
import io.aeron.driver.MediaDriver;
import io.aeron.driver.ThreadingMode;
import io.aeron.logbuffer.FragmentHandler;
import io.aeron.shadow.org.HdrHistogram.Histogram;
import io.aeron.shadow.org.HdrHistogram.HistogramLogWriter;
import org.agrona.CloseHelper;
import org.agrona.DirectBuffer;
import org.agrona.collections.MutableLong;
import org.agrona.concurrent.BackoffIdleStrategy;
import org.agrona.concurrent.IdleStrategy;
import org.agrona.concurrent.UnsafeBuffer;
import org.agrona.concurrent.status.CountersReader;

import java.io.File;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

public final class QualificationHarness
{
    private static final String VERSION = "1.1.0";
    private static final String IPC_CHANNEL = "aeron:ipc";
    private static final String ARCHIVE_CONTROL_REQUEST_CHANNEL = "aeron:udp?endpoint=localhost:8010";
    private static final String ARCHIVE_CONTROL_RESPONSE_CHANNEL = "aeron:udp?endpoint=localhost:0";
    private static final String ARCHIVE_REPLICATION_CHANNEL = "aeron:udp?endpoint=localhost:0";
    private static final int TERM_BUFFER_LENGTH = 16 * 1024 * 1024;
    private static final int SEGMENT_FILE_LENGTH = 16 * 1024 * 1024;
    private static final int IDLE_MAX_SPINS = 100;
    private static final int IDLE_MAX_YIELDS = 10;
    private static final long IDLE_MIN_PARK_NS = 1;
    private static final long IDLE_MAX_PARK_NS = 100_000;
    private static final int RECORDING_STREAM_ID = 1001;
    private static final int REPLAY_STREAM_ID = 1002;
    private static final long TIMEOUT_NS = TimeUnit.SECONDS.toNanos(30);
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final int MARKER_OFFSET = 16;
    private static final int MARKER_LENGTH = 64;

    private QualificationHarness()
    {
    }

    public static void main(final String[] args) throws Exception
    {
        if (args.length == 0)
        {
            fail("command is required");
        }

        final String command = args[0];
        final Map<String, String> options = parseOptions(args);
        switch (command)
        {
            case "version" -> version();
            case "server" -> server(options);
            case "health" -> health(options);
            case "record" -> record(options);
            case "replay" -> replay(options);
            case "ipc" -> ipc(options);
            default -> fail("unsupported command: " + command);
        }
    }

    private static void version()
    {
        System.out.printf(
            "{\"schema\":\"aeron-native-harness-version/v1\",\"harness\":\"%s\",\"aeron\":\"1.52.2\",\"java_vendor\":\"%s\",\"java_version\":\"%s\"}%n",
            VERSION, json(System.getProperty("java.vendor")), json(System.getProperty("java.version")));
    }

    private static void server(final Map<String, String> options) throws Exception
    {
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final Path driverDir = root.resolve("driver");
        final Path archiveDir = root.resolve("archive");
        Files.createDirectories(driverDir);
        Files.createDirectories(archiveDir);

        final int fileSyncLevel = integer(options, "file-sync-level", 1);
        final int catalogSyncLevel = integer(options, "catalog-sync-level", 1);
        final CountDownLatch shutdown = new CountDownLatch(1);
        final AtomicBoolean closed = new AtomicBoolean();

        final MediaDriver.Context driverContext = new MediaDriver.Context()
            .aeronDirectoryName(driverDir.toString())
            .dirDeleteOnStart(false)
            .dirDeleteOnShutdown(false)
            .spiesSimulateConnection(true)
            .threadingMode(ThreadingMode.SHARED_NETWORK)
            .termBufferSparseFile(false)
            .publicationTermBufferLength(TERM_BUFFER_LENGTH)
            .ipcTermBufferLength(TERM_BUFFER_LENGTH)
            .sharedNetworkIdleStrategy(newIdleStrategy());
        final Archive.Context archiveContext = new Archive.Context()
            .aeronDirectoryName(driverDir.toString())
            .archiveDir(archiveDir.toFile())
            .deleteArchiveOnStart(false)
            .recordingEventsEnabled(false)
            .threadingMode(ArchiveThreadingMode.SHARED)
            .idleStrategySupplier(QualificationHarness::newIdleStrategy)
            .recorderIdleStrategySupplier(QualificationHarness::newIdleStrategy)
            .replayerIdleStrategySupplier(QualificationHarness::newIdleStrategy)
            .controlChannel(ARCHIVE_CONTROL_REQUEST_CHANNEL)
            .localControlChannel(IPC_CHANNEL)
            .recordingEventsChannel(ARCHIVE_CONTROL_RESPONSE_CHANNEL)
            .replicationChannel(ARCHIVE_REPLICATION_CHANNEL)
            .segmentFileLength(SEGMENT_FILE_LENGTH)
            .fileSyncLevel(fileSyncLevel)
            .catalogFileSyncLevel(catalogSyncLevel);

        final ArchivingMediaDriver driver = ArchivingMediaDriver.launch(driverContext, archiveContext);
        final Runnable close = () ->
        {
            if (closed.compareAndSet(false, true))
            {
                CloseHelper.quietClose(driver);
                shutdown.countDown();
            }
        };
        Runtime.getRuntime().addShutdownHook(new Thread(close, "aeron-server-shutdown"));

        try (Aeron aeron = Aeron.connect(new Aeron.Context().aeronDirectoryName(driverDir.toString()));
            AeronArchive archive = AeronArchive.connect(archiveClientContext(aeron)))
        {
            final Path ready = root.resolve("server-ready.json");
            Files.writeString(ready, String.format(
                "{\"schema\":\"aeron-server-ready/v2\",\"pid\":%d,\"started_at\":\"%s\",\"driver_dir\":\"%s\",\"archive_dir\":\"%s\",\"archive_id\":%d,\"file_sync_level\":%d,\"catalog_sync_level\":%d,\"term_buffer_length\":%d,\"segment_file_length\":%d,\"driver_threading\":\"SHARED_NETWORK\",\"archive_threading\":\"SHARED\",\"idle_strategy\":\"backoff-100-10-1-100000ns\",\"sparse\":false}%n",
                ProcessHandle.current().pid(), Instant.now(), json(driverDir.toString()), json(archiveDir.toString()),
                archive.archiveId(), fileSyncLevel, catalogSyncLevel, TERM_BUFFER_LENGTH, SEGMENT_FILE_LENGTH),
                StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
            System.out.print(Files.readString(ready));
            System.out.flush();
        }

        shutdown.await();
    }

    private static void health(final Map<String, String> options)
    {
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final String driverDir = root.resolve("driver").toString();
        try (Aeron aeron = Aeron.connect(new Aeron.Context().aeronDirectoryName(driverDir));
            AeronArchive archive = AeronArchive.connect(archiveClientContext(aeron)))
        {
            System.out.printf(
                "{\"schema\":\"aeron-live-health/v1\",\"status\":\"live\",\"checked_at\":\"%s\",\"archive_id\":%d}%n",
                Instant.now(), archive.archiveId());
        }
    }

    private static void record(final Map<String, String> options) throws Exception
    {
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final int count = positive(options, "count");
        final int payload = integer(options, "payload", 64);
        final byte[] marker = requiredMarker(options);
        if (payload < MARKER_OFFSET + MARKER_LENGTH)
        {
            fail("--payload must leave room for the 64-byte marker");
        }
        final String receipt = options.getOrDefault("receipt", "durable_group");
        if (!receipt.equals("visible") && !receipt.equals("durable_group") && !receipt.equals("durable_sync"))
        {
            fail("receipt must be visible, durable_group, or durable_sync");
        }

        final String driverDir = root.resolve("driver").toString();
        final UnsafeBuffer buffer = new UnsafeBuffer(ByteBuffer.allocateDirect(Math.max(payload, 16)));
        final MutableLong expected = new MutableLong();
        final MutableLong duplicates = new MutableLong();
        final MutableLong reordered = new MutableLong();
        final MutableLong markerMismatches = new MutableLong();
        final FragmentHandler handler = (data, offset, length, header) ->
        {
            final long sequence = data.getLong(offset);
            if (sequence < expected.get())
            {
                duplicates.increment();
            }
            else if (sequence != expected.get())
            {
                reordered.increment();
                expected.set(sequence + 1);
            }
            else
            {
                expected.increment();
            }
            if (length < MARKER_OFFSET + MARKER_LENGTH ||
                !matchesMarker(data, offset + MARKER_OFFSET, marker))
            {
                markerMismatches.increment();
            }
        };

        long backpressure = 0;
        long finalPosition;
        long recordingId;
        long receiptPosition;
        long receiptDurationNs;
        final long startNs = System.nanoTime();
        try (Aeron aeron = Aeron.connect(new Aeron.Context().aeronDirectoryName(driverDir));
            AeronArchive archive = AeronArchive.connect(archiveClientContext(aeron));
            ExclusivePublication publication = aeron.addExclusivePublication(IPC_CHANNEL, RECORDING_STREAM_ID))
        {
            final String sessionChannel = ChannelUri.addSessionId(IPC_CHANNEL, publication.sessionId());
            final long subscriptionId = archive.startRecording(sessionChannel, RECORDING_STREAM_ID, SourceLocation.LOCAL);
            try (Subscription subscription = aeron.addSubscription(sessionChannel, RECORDING_STREAM_ID))
            {
                await(() -> publication.isConnected() && subscription.isConnected(), "publication connection");
                final CountersReader counters = aeron.countersReader();
                final int counterId = awaitRecordingCounter(counters, publication.sessionId(), archive.archiveId());
                recordingId = RecordingPos.getRecordingId(counters, counterId);

                for (int sequence = 0; sequence < count; sequence++)
                {
                    buffer.putLong(0, sequence);
                    buffer.putLong(8, System.nanoTime());
                    buffer.putBytes(MARKER_OFFSET, marker);
                    final long offerDeadline = System.nanoTime() + TIMEOUT_NS;
                    while (publication.offer(buffer, 0, payload) < 0)
                    {
                        backpressure++;
                        subscription.poll(handler, 32);
                        if (System.nanoTime() > offerDeadline)
                        {
                            fail("offer timeout at sequence " + sequence);
                        }
                        Thread.onSpinWait();
                    }
                    subscription.poll(handler, 32);
                    if (receipt.equals("durable_sync"))
                    {
                        awaitCounter(counters, counterId, publication.position());
                    }
                }

                awaitPoll(subscription, handler, expected, count);
                receiptPosition = publication.position();
                if (!receipt.equals("visible"))
                {
                    awaitCounter(counters, counterId, receiptPosition);
                }
                receiptDurationNs = System.nanoTime() - startNs;
                finalPosition = publication.position();
                awaitCounter(counters, counterId, finalPosition);
            }
            finally
            {
                archive.stopRecording(subscriptionId);
            }
        }

        final long completionDurationNs = System.nanoTime() - startNs;
        if (duplicates.get() != 0 || reordered.get() != 0 || markerMismatches.get() != 0 || expected.get() != count)
        {
            fail("record oracle failed");
        }
        System.out.printf(
            "{\"schema\":\"aeron-record-receipt/v2\",\"recording_id\":%d,\"count\":%d,\"observed\":%d,\"duplicates\":%d,\"reordered\":%d,\"marker_mismatches\":%d,\"marker\":\"%s\",\"receipt\":\"%s\",\"receipt_position\":%d,\"final_position\":%d,\"receipt_duration_ns\":%d,\"completion_duration_ns\":%d,\"backpressure\":%d}%n",
            recordingId, count, expected.get(), duplicates.get(), reordered.get(), markerMismatches.get(),
            new String(marker, StandardCharsets.US_ASCII), receipt, receiptPosition, finalPosition,
            receiptDurationNs, completionDurationNs, backpressure);
    }

    private static void replay(final Map<String, String> options) throws Exception
    {
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final int count = positive(options, "count");
        final long recordingId = nonNegativeLongValue(options, "recording-id");
        final long length = longValue(options, "length");
        final byte[] marker = requiredMarker(options);
        final String driverDir = root.resolve("driver").toString();
        final MutableLong expected = new MutableLong();
        final MutableLong duplicates = new MutableLong();
        final MutableLong reordered = new MutableLong();
        final MutableLong markerMismatches = new MutableLong();
        final FragmentHandler handler = (data, offset, messageLength, header) ->
        {
            final long sequence = data.getLong(offset);
            if (sequence < expected.get())
            {
                duplicates.increment();
            }
            else if (sequence != expected.get())
            {
                reordered.increment();
                expected.set(sequence + 1);
            }
            else
            {
                expected.increment();
            }
            if (messageLength < MARKER_OFFSET + MARKER_LENGTH ||
                !matchesMarker(data, offset + MARKER_OFFSET, marker))
            {
                markerMismatches.increment();
            }
        };

        final long startNs = System.nanoTime();
        try (Aeron aeron = Aeron.connect(new Aeron.Context().aeronDirectoryName(driverDir));
            AeronArchive archive = AeronArchive.connect(archiveClientContext(aeron));
            Subscription replay = archive.replay(recordingId, 0, length, IPC_CHANNEL, REPLAY_STREAM_ID))
        {
            await(replay::isConnected, "replay connection");
            final long deadline = System.nanoTime() + TIMEOUT_NS;
            while (expected.get() < count)
            {
                replay.poll(handler, 64);
                if (System.nanoTime() > deadline)
                {
                    fail("replay timeout at " + expected.get() + " of " + count);
                }
                Thread.onSpinWait();
            }
        }

        if (duplicates.get() != 0 || reordered.get() != 0 || markerMismatches.get() != 0 || expected.get() != count)
        {
            fail("replay oracle failed");
        }
        System.out.printf(
            "{\"schema\":\"aeron-replay-receipt/v2\",\"recording_id\":%d,\"expected\":%d,\"observed\":%d,\"duplicates\":0,\"reordered\":0,\"marker_mismatches\":0,\"marker\":\"%s\",\"duration_ns\":%d}%n",
            recordingId, count, expected.get(), new String(marker, StandardCharsets.US_ASCII),
            System.nanoTime() - startNs);
    }

    private static void ipc(final Map<String, String> options) throws Exception
    {
        final Path root = requiredPath(options, "root").toAbsolutePath();
        final int warmup = integer(options, "warmup", 1000);
        final int messages = positive(options, "messages");
        final int payload = integer(options, "payload", 64);
        final long rate = longValue(options, "rate");
        final Path histogramPath = requiredPath(options, "histogram").toAbsolutePath();
        Files.createDirectories(root);
        Files.createDirectories(histogramPath.getParent());

        final String driverDir = root.resolve("driver").toString();
        final MediaDriver.Context context = new MediaDriver.Context()
            .aeronDirectoryName(driverDir)
            .dirDeleteOnStart(true)
            .dirDeleteOnShutdown(true)
            .threadingMode(ThreadingMode.SHARED_NETWORK)
            .spiesSimulateConnection(true)
            .termBufferSparseFile(false)
            .publicationTermBufferLength(TERM_BUFFER_LENGTH)
            .ipcTermBufferLength(TERM_BUFFER_LENGTH)
            .sharedNetworkIdleStrategy(newIdleStrategy());
        final Histogram histogram = new Histogram(TimeUnit.SECONDS.toNanos(10), 3);
        long backpressure = 0;
        long pollFailures = 0;
        final long expectedInterval = Math.max(1, TimeUnit.SECONDS.toNanos(1) / rate);
        final UnsafeBuffer buffer = new UnsafeBuffer(ByteBuffer.allocateDirect(Math.max(payload, 16)));

        try (MediaDriver driver = MediaDriver.launch(context);
            Aeron aeron = Aeron.connect(new Aeron.Context().aeronDirectoryName(driverDir));
            ExclusivePublication publication = aeron.addExclusivePublication(IPC_CHANNEL, 2001);
            Subscription subscription = aeron.addSubscription(IPC_CHANNEL, 2001))
        {
            await(() -> publication.isConnected() && subscription.isConnected(), "IPC connection");
            final long[] received = new long[1];
            final FragmentHandler handler = (data, offset, length, header) ->
            {
                final long sentNs = data.getLong(offset + 8);
                received[0] = data.getLong(offset) + 1;
                if (sentNs != 0)
                {
                    histogram.recordValueWithExpectedInterval(Math.max(1, System.nanoTime() - sentNs), expectedInterval);
                }
            };

            for (int i = -warmup; i < messages; i++)
            {
                if (i == 0)
                {
                    histogram.reset();
                    histogram.setStartTimeStamp(System.currentTimeMillis());
                    received[0] = 0;
                }
                final long sequence = Math.max(0, i);
                buffer.putLong(0, sequence);
                buffer.putLong(8, i < 0 ? 0 : System.nanoTime());
                while (publication.offer(buffer, 0, payload) < 0)
                {
                    backpressure++;
                    Thread.onSpinWait();
                }
                final long deadline = System.nanoTime() + TIMEOUT_NS;
                while (subscription.poll(handler, 1) == 0)
                {
                    pollFailures++;
                    if (System.nanoTime() > deadline)
                    {
                        fail("IPC poll timeout");
                    }
                    Thread.onSpinWait();
                }
                if (i >= 0)
                {
                    final long target = buffer.getLong(8) + expectedInterval;
                    while (System.nanoTime() < target)
                    {
                        Thread.onSpinWait();
                    }
                }
            }
        }
        histogram.setEndTimeStamp(System.currentTimeMillis());

        try (PrintStream stream = new PrintStream(Files.newOutputStream(histogramPath)))
        {
            final HistogramLogWriter writer = new HistogramLogWriter(stream);
            writer.outputLogFormatVersion();
            writer.outputStartTime(histogram.getStartTimeStamp());
            writer.outputLegend();
            writer.outputIntervalHistogram(histogram);
        }

        System.out.printf(
            "{\"schema\":\"aeron-ipc-measurement/v1\",\"messages\":%d,\"payload\":%d,\"offered_rate\":%d,\"samples\":%d,\"p50_ns\":%d,\"p95_ns\":%d,\"p99_ns\":%d,\"p999_ns\":%d,\"max_ns\":%d,\"backpressure\":%d,\"poll_failures\":%d,\"coordinated_omission\":\"expected-interval-correction\",\"histogram\":\"%s\",\"histogram_start_time_ms\":%d,\"histogram_end_time_ms\":%d}%n",
            messages, payload, rate, histogram.getTotalCount(), histogram.getValueAtPercentile(50),
            histogram.getValueAtPercentile(95), histogram.getValueAtPercentile(99),
            histogram.getValueAtPercentile(99.9), histogram.getMaxValue(), backpressure, pollFailures,
            json(histogramPath.toString()), histogram.getStartTimeStamp(), histogram.getEndTimeStamp());
    }

    private static int awaitRecordingCounter(
        final CountersReader counters, final int sessionId, final long archiveId)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        int counterId;
        while (Aeron.NULL_VALUE ==
            (counterId = RecordingPos.findCounterIdBySession(counters, sessionId, archiveId)))
        {
            if (System.nanoTime() > deadline)
            {
                fail("recording counter timeout");
            }
            Thread.onSpinWait();
        }
        return counterId;
    }

    private static void awaitCounter(final CountersReader counters, final int counterId, final long position)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        while (counters.getCounterValue(counterId) < position)
        {
            if (System.nanoTime() > deadline)
            {
                fail("recording position timeout");
            }
            Thread.onSpinWait();
        }
    }

    private static void awaitPoll(
        final Subscription subscription, final FragmentHandler handler, final MutableLong observed, final long expected)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        while (observed.get() < expected)
        {
            subscription.poll(handler, 64);
            if (System.nanoTime() > deadline)
            {
                fail("visibility timeout at " + observed.get() + " of " + expected);
            }
            Thread.onSpinWait();
        }
    }

    private static void await(final BooleanSupplier condition, final String context)
    {
        final long deadline = System.nanoTime() + TIMEOUT_NS;
        while (!condition.getAsBoolean())
        {
            if (System.nanoTime() > deadline)
            {
                fail(context + " timeout");
            }
            Thread.onSpinWait();
        }
    }

    private static Map<String, String> parseOptions(final String[] args)
    {
        final Map<String, String> options = new HashMap<>();
        for (int i = 1; i < args.length; i += 2)
        {
            if (!args[i].startsWith("--") || i + 1 >= args.length)
            {
                fail("options must be --name value pairs");
            }
            options.put(args[i].substring(2), args[i + 1]);
        }
        return options;
    }

    private static Path requiredPath(final Map<String, String> options, final String name)
    {
        final String value = options.get(name);
        if (value == null || value.isBlank())
        {
            fail("--" + name + " is required");
        }
        return Path.of(value);
    }

    private static int positive(final Map<String, String> options, final String name)
    {
        final int value = integer(options, name, -1);
        if (value <= 0)
        {
            fail("--" + name + " must be positive");
        }
        return value;
    }

    private static int integer(final Map<String, String> options, final String name, final int defaultValue)
    {
        final String value = options.get(name);
        return value == null ? defaultValue : Integer.parseInt(value);
    }

    private static long longValue(final Map<String, String> options, final String name)
    {
        final String value = options.get(name);
        if (value == null)
        {
            fail("--" + name + " is required");
        }
        final long parsed = Long.parseLong(value);
        if (parsed <= 0)
        {
            fail("--" + name + " must be positive");
        }
        return parsed;
    }

    private static long nonNegativeLongValue(final Map<String, String> options, final String name)
    {
        final String value = options.get(name);
        if (value == null)
        {
            fail("--" + name + " is required");
        }
        final long parsed = Long.parseLong(value);
        if (parsed < 0)
        {
            fail("--" + name + " must be non-negative");
        }
        return parsed;
    }

    private static byte[] requiredMarker(final Map<String, String> options)
    {
        final String marker = options.get("marker");
        if (marker == null || !SHA256.matcher(marker).matches())
        {
            fail("--marker must be a lowercase SHA-256");
        }
        return marker.getBytes(StandardCharsets.US_ASCII);
    }

    private static AeronArchive.Context archiveClientContext(final Aeron aeron)
    {
        return new AeronArchive.Context()
            .aeron(aeron)
            .controlRequestChannel(ARCHIVE_CONTROL_REQUEST_CHANNEL)
            .controlResponseChannel(ARCHIVE_CONTROL_RESPONSE_CHANNEL);
    }

    private static IdleStrategy newIdleStrategy()
    {
        return new BackoffIdleStrategy(
            IDLE_MAX_SPINS, IDLE_MAX_YIELDS, IDLE_MIN_PARK_NS, IDLE_MAX_PARK_NS);
    }

    private static boolean matchesMarker(final DirectBuffer data, final int offset, final byte[] expected)
    {
        if (data.capacity() < offset + expected.length)
        {
            return false;
        }
        for (int index = 0; index < expected.length; index++)
        {
            if (data.getByte(offset + index) != expected[index])
            {
                return false;
            }
        }
        return true;
    }

    private static String json(final String value)
    {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static void fail(final String message)
    {
        throw new IllegalStateException(message);
    }

    @FunctionalInterface
    private interface BooleanSupplier
    {
        boolean getAsBoolean();
    }
}
