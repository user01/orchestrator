# Orchestrator

`orch` is a simple process orchestrator that runs a set of tasks defined in a TOML file. It provides a Textual-based user interface for visualizing the state of the tasks and their logs.

## Features

*   **Task dependencies:** Define dependencies between tasks, ensuring that they are run in the correct order.
*   **Different task kinds:** Supports "oneshot", "service", and "daemon" tasks.
*   **Readiness probes:** Check if a service is ready before starting dependent tasks.
*   **Textual-based UI:** A rich user interface for visualizing the state of the tasks and their logs.
*   **Log filtering:** Filter the logs to show only the output of a single task.
*   **Send stdin:** Send input to running tasks.

## Installation

```bash
uv build
uv tool install dist/orch*.whl
```

## Usage

To run the orchestrator, you will need to create a `orchestrate.toml` file that defines the tasks to run. You can then run the orchestrator using the following command:

```
orch
```

You can also specify a different configuration file using the following command:

```
orch my_config.toml
```

### Generating a sample configuration file

To generate a sample configuration file, you can use the `--sample-toml` argument:

```
orch --sample-toml > orchestrate.toml
```

This will create a `orchestrate.toml` file in the current directory with a sample configuration that you can modify to suit your needs.

## Configuration

The configuration for the orchestrator is defined in a TOML file. The file consists of a `defaults` table and a list of `task` tables.

### The `defaults` table

The `defaults` table can be used to specify default values for the tasks. The following options are available:

*   `workdir`: The working directory for the tasks. Defaults to the current directory.

### The `task` table

Each `task` table defines a single task to run. The following options are available:

*   `name`: The name of the task.
*   `kind`: The kind of the task. Can be one of "oneshot", "service", or "daemon".
*   `cmd`: The command to run for the task.
*   `depends_on`: A list of task names that this task depends on.
*   `ready_cmd`: A command to run to check if a service task is ready.
*   `workdir`: The working directory for the task. Overrides the default.

## Task kinds

The following task kinds are available:

*   `oneshot`: A task that runs to completion and then exits.
*   `service`: A long-running process that is expected to stay alive. It becomes "READY" when a readiness probe succeeds.
*   `daemon`: A long-running process that is not expected to exit. It is considered ready as soon as it starts.
