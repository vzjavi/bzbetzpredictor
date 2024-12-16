$(document).ready(function () {
    // Initialize Select2 for searchable dropdowns
    $('.searchable').select2();

    const sportSelect = $('#sheet_name');
    const team1Select = $('#team1');
    const team2Select = $('#team2');

    // Function to fetch and populate teams
    function populateTeams(sport) {
        $.ajax({
            url: '/get-teams',
            type: 'GET',
            success: function (data) {
                // Clear previous options
                team1Select.empty().append('<option value="" disabled selected>Choose Team 1</option>');
                team2Select.empty().append('<option value="" disabled selected>Choose Team 2</option>');

                if (data[sport]) {
                    // Populate Team 1 and Team 2 dropdowns
                    data[sport].forEach(team => {
                        team1Select.append(new Option(team, team));
                        team2Select.append(new Option(team, team));
                    });
                }

                // Refresh Select2 after adding options
                team1Select.trigger('change');
                team2Select.trigger('change');
            },
            error: function (error) {
                console.error('Error fetching teams:', error);
            }
        });
    }

    // Event listener for sport selection
    sportSelect.on('change', function () {
        const selectedSport = $(this).val();
        populateTeams(selectedSport);
    });
});
console.log("script.js loaded!");
