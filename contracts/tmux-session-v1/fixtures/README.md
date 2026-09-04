# Tmux Session Contract v1 fixtures

These deterministic fixtures are producer-owned JSON examples for consumers.
They deliberately contain only logical identities and no host routes, local
paths, account names, or private state. The executable tests use a generated
`tmux -L` socket and never inspect or mutate the user's default server.
